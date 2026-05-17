import os
import re
import threading

from exllamav2 import (
    ExLlamaV2,
    ExLlamaV2Config,
    ExLlamaV2Tokenizer,
    ExLlamaV2Cache_Q8,
)
from exllamav2.generator import ExLlamaV2BaseGenerator, ExLlamaV2Sampler


MODEL_DIR = os.environ.get("MODEL_DIR", "/home/daniel/models/qwen14b-exl2-v2")
MAX_SEQ_LEN = int(os.environ.get("MAX_SEQ_LEN", "8192"))


class Qwen14BEngine:
    def __init__(self):
        print(
            f"[DEBUG] Qwen14BEngine.__init__ start "
            f"pid={os.getpid()} tid={threading.get_ident()}",
            flush=True,
        )
        print("Loading Qwen 14B...")
        print(
            f"[DEBUG] startup model_dir={MODEL_DIR} max_seq_len={MAX_SEQ_LEN}",
            flush=True,
        )

        config = ExLlamaV2Config()
        config.model_dir = MODEL_DIR
        config.max_seq_len = MAX_SEQ_LEN
        print(f"[DEBUG] ExLlamaV2Config prepared, calling prepare()", flush=True)
        config.prepare()

        print(f"[DEBUG] ExLlamaV2 model object create", flush=True)
        self.model = ExLlamaV2(config)
        print(f"[DEBUG] ExLlamaV2Tokenizer create", flush=True)
        self.tokenizer = ExLlamaV2Tokenizer(config)

        print(f"[DEBUG] ExLlamaV2Cache_Q8 create  seq_len={MAX_SEQ_LEN}", flush=True)
        self.cache = ExLlamaV2Cache_Q8(self.model, max_seq_len=MAX_SEQ_LEN)
        print(f"[DEBUG] load_autosplit start", flush=True)
        self.model.load_autosplit(self.cache)
        print(f"[DEBUG] load_autosplit done", flush=True)

        # BaseGenerator
        print(f"[DEBUG] ExLlamaV2BaseGenerator create", flush=True)
        self.generator = ExLlamaV2BaseGenerator(self.model, self.cache, self.tokenizer)
        print(f"[DEBUG] ExLlamaV2BaseGenerator done", flush=True)

        print("Qwen 14B loaded")

    def generate(self, prompt: str, max_new_tokens: int = 512) -> str:
        settings = ExLlamaV2Sampler.Settings()
        settings.temperature = 0.7
        settings.top_p = 0.9
        settings.top_k = 50

        output = self.generator.generate_simple(
            prompt,
            settings,
            num_tokens=max_new_tokens,
            completion_only=True,
        )

        # Qwen3 thinking mode: strip <think>...</think> blocks before returning.
        # The model sometimes omits the opening tag, so also strip anything
        # before a bare </think> closing tag.
        output = re.sub(r"<think>.*?</think>", "", output, flags=re.DOTALL)
        output = re.sub(r"^.*?</think>", "", output, flags=re.DOTALL)
        output = output.strip()

        return output


engine = Qwen14BEngine()