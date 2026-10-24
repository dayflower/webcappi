GO ?= go
IMAGE ?= webcappi:dev
PLATFORM ?=
PLATFORMS ?= linux/amd64,linux/arm64

.PHONY: build test image image-multi binaries browser-test
build:
	$(GO) build -trimpath -o bin/webcappi ./cmd/webcappi

test:
	$(GO) test -race ./...

image:
	docker buildx build --load $(if $(PLATFORM),--platform $(PLATFORM)) -t "$(IMAGE)" .

image-multi:
	mkdir -p artifacts
	docker buildx build --platform "$(PLATFORMS)" --output type=oci,dest=artifacts/webcappi.oci.tar -t "$(IMAGE)" .

binaries:
	docker buildx build --target binary --platform "$(PLATFORMS)" --output type=local,dest=artifacts/binaries .

browser-test:
	bash scripts/browser-test.sh
