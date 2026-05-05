# Load Test do Sock Shop (Locust)

Este documento descreve, em detalhe, como o script de carga em `loadtest-configmap.yaml` funciona hoje.

Escopo deste README:
- Explicar o fluxo completo de **um usuário virtual**.
- Explicar como os dados do usuário são armazenados/validados ao longo da jornada.
- Explicar como o Locust simula vários usuários em paralelo (modelo de concorrência).
- Destacar onde podem surgir chamadas como `/customers/undefined`.

## 1. Onde o script roda

No arquivo `loadtest-configmap.yaml` existem 3 recursos principais:
- `ConfigMap locust-script`: contém o `locustfile.py` montado em `/mnt/locust/locustfile.py`.
- `Deployment locust-web`: executa `locustio/locust:2.27.0` em modo web (`--web-port 8089`).
- `Service locust-web`: expõe a UI/API do Locust no namespace `loadtest`.

Host-alvo do teste:
- `TARGET_HOST=http://front-end.sock-shop.svc.cluster.local`

## 2. Modelo de concorrência do Locust ("threads")

Importante: Locust não usa threads de SO por usuário. Ele usa **greenlets** (concorrência cooperativa via `gevent`).

Na prática:
- Cada `SockShopUser` é uma instância isolada com estado próprio.
- Cada usuário tem seu próprio client HTTP (`self.client`) e seu próprio cookie jar/sessão.
- O paralelismo é controlado por:
  - `user_count`: total de usuários virtuais ativos.
  - `spawn_rate`: taxa de criação de usuários por segundo.
- O scheduler do Locust escolhe tasks conforme peso (`@task(n)`).

Consequência prática:
- `self.customer_id`, `self.username`, etc. são por usuário virtual, não globais.
- Falha de sessão de um usuário não deveria contaminar outro usuário.

## 3. Estado interno de um usuário virtual

A classe `SockShopUser(HttpUser)` mantém estes campos principais:
- `self.username`
- `self.password`
- `self.email`
- `self.customer_id`
- `self.enable_checkout` (hoje `True`)

### Inicialização (`on_start`)

Quando o usuário nasce:
1. `_new_identity()` gera credenciais únicas.
2. `_register()` cria o usuário no backend.
3. `_login()` autentica no front-end para fixar sessão HTTP com customer.
4. `enable_checkout = True` habilita tentativa de checkout na jornada.

## 4. Fluxo lógico de dados de identidade

## 4.1 Geração de identidade
`_new_identity()`:
- Gera `username` com `uuid4`.
- Define senha fixa (`Passw0rd!`) e email derivado.
- Reseta `customer_id = None`.

## 4.2 Registro
`_register()`:
- `POST /register` com `username/password/email`.
- Espera `200/201`.
- Tenta extrair `id` ou `_id` da resposta.
- Só persiste em `self.customer_id` se for ObjectId válido (`24 hex`).

## 4.3 Login
`_login()`:
- Chama `GET /login` com Basic Auth (`username:password`) e header `X-Requested-With`.
- Espera status `200` e ausência de erro de aplicação.
- Tenta extrair `customer_id` do body (`id`, `_id`, ou `_links.customer.href`).
- Se não vier ID no body, aceita fallback para `self.customer_id` já válido.
- Em falha, retorna `False` e limpa `self.customer_id` quando necessário.

Objetivo do login:
- Garantir que a sessão HTTP do front-end esteja associada ao customer correto.

## 5. Fluxo da jornada real (`realistic_journey`)

Task com peso `4` (mais frequente que registro).

Sequência:
1. Se `self.customer_id` vazio, retorna (não prossegue).
2. Com probabilidade de 25%, faz refresh de sessão via `_login()`.
   - Se falhar: recria identidade (`_new_identity` + `_register`) e tenta `_login()` novamente.
   - Se segunda tentativa falhar: aborta a jornada.
3. Navegação:
   - `GET /`
   - `GET /category.html`
4. Catálogo:
   - `_pick_catalogue_item()` faz `GET /catalogue?size=9`
   - Escolhe item aleatório e retorna `item_id`.
5. Carrinho:
   - `_clear_cart()` (`DELETE /cart`)
   - `_add_to_cart(item_id)` (`POST /cart`)
6. Cesta:
   - `GET /basket.html`
7. Checkout (se `item_id` e `enable_checkout=True`):
   - `_create_address()` (`POST /addresses`) e valida ID.
   - `_create_card()` (`POST /cards`) e valida ID.
   - `_get_default_address()` (`GET /address`) deve retornar 200.
   - `_get_default_card()` (`GET /card`) deve retornar 200.
   - `_checkout()` (`POST /orders`) deve retornar 200/201/202 e sem erro de aplicação.
8. Pós-checkout:
   - Só chama `GET /customer-orders.html` se `checkout_done=True`.

## 6. Como os itens e dados do usuário são recuperados

Ponto importante:
- O item do catálogo (`item_id`) não depende diretamente do `customer_id`; vem de `GET /catalogue`.
- Já operações de carrinho/endereço/cartão/pedido dependem do contexto de sessão autenticada no front-end.

Em outras palavras:
- `customer_id` é usado indiretamente, via sessão/cookies do front-end.
- O script valida IDs de address/card (ObjectId) para evitar payload inconsistente.
- O checkout final é feito sem enviar IDs no body (`POST /orders`), confiando na sessão.

## 7. Onde nasce o problema `/customers/undefined`

Esse padrão costuma aparecer quando a sessão no front-end não está íntegra para o usuário atual.

Cenários típicos:
- Login falha/intermitente e a navegação continua.
- Customer da sessão não foi corretamente associado ao usuário criado.
- Página do front-end que consulta customer é chamada sem contexto válido.

Defesas já presentes no script:
- `_login()` com retorno booleano e validações.
- Recuperação de identidade quando refresh de login falha.
- Validação de resposta/erro de aplicação nos pontos críticos.
- `GET /customer-orders.html` somente após checkout concluído.

## 8. Tasks e pesos

Tasks definidas:
- `register_new_user` com peso `1`:
  - recria identidade, registra e loga.
- `realistic_journey` com peso `4`:
  - executa fluxo de compra.

Interpretação:
- Em média, para cada 5 execuções de task, ~4 tendem a ser jornada e ~1 registro.

## 9. Validações de integridade no script

Validações centrais:
- Status HTTP esperado por endpoint.
- `error` de aplicação no JSON (`_response_has_app_error`).
- IDs de recursos no formato ObjectId (`_is_valid_object_id`).
- Extração de ID por `id`, `_id`, ou links HAL (`_extract_object_id`).

Quando uma validação falha:
- A requisição é marcada como `failure` no Locust.
- Em pontos críticos, a jornada é interrompida para evitar propagar estado inconsistente.

## 10. Resumo operacional

Para funcionar bem no cenário de "jornada real com pedido":
- O usuário precisa nascer com identidade única.
- Registro e login precisam consolidar sessão do front-end.
- Catálogo/carrinho precisam manter coerência com a sessão.
- Address/card precisam ser criados e ficar disponíveis como default.
- Checkout (`POST /orders`) depende dessa cadeia estar íntegra.

Quando houver quebra do front-end, a análise deve começar por:
1. Taxa de falha em `GET /login`.
2. Ocorrências de `/customers/undefined` no `user` service.
3. Taxa de sucesso de `POST /orders`.
4. Se o usuário está navegando para páginas de conta sem sessão válida.
