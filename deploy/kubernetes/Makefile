PWD = $(shell pwd)
MANIFESTS = $(or $(shell printenv MANIFESTS), manifests)

.PHONY: gen-complete-demo
gen-complete-demo:
	awk '{print}' ${MANIFESTS}/* > complete-demo.yaml
.PHONY: check-complete-demo
check-complete-demo:
	awk '{print}' ${MANIFESTS}/* | diff complete-demo.yaml -

.PHONY: ensure-dockerimage
ensure-dockerimage: Dockerfile
	@docker image inspect manifests-image >/dev/null 2>&1 || docker build -t manifests-image .

.PHONY: docker-%
docker-%: ensure-dockerimage
	docker \
	  run \
	  --rm \
	  -it \
	  -u ${UID}:${GID} \
	  -v ${PWD}:/workdir \
	  manifests-image \
	  make $*
