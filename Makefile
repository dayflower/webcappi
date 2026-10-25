GO ?= go
STATICCHECK ?= staticcheck
RUFF ?= ruff
IMAGE ?= webcappi:dev
PLATFORM ?=
PLATFORMS ?= linux/amd64,linux/arm64

.PHONY: build test image image-multi binaries browser-test redis-test
.PHONY: format-check format-check-go format-check-python format format-go format-python
.PHONY: lint lint-go lint-python
build:
	$(GO) build -trimpath -o bin/webcappi ./cmd/webcappi

test:
	$(GO) test -race ./...

format-check: format-check-go format-check-python

format-check-go:
	@goroot=$$($(GO) env GOROOT) || exit $$?; \
	files=$$("$$goroot/bin/gofmt" -l cmd internal) || exit $$?; \
	if [ -n "$$files" ]; then \
		printf '%s\n' "$$files"; \
		printf '%s\n' 'Run make format-go to fix Go formatting.' >&2; \
		exit 1; \
	fi

format-check-python:
	$(RUFF) format --check scripts/

format: format-go format-python

format-go:
	$(GO) fmt ./cmd/... ./internal/...

format-python:
	$(RUFF) format scripts/

lint: lint-go lint-python

lint-go:
	$(GO) vet ./cmd/... ./internal/...
	$(STATICCHECK) ./cmd/... ./internal/...

lint-python:
	$(RUFF) check scripts/

image:
	docker buildx build --load $(if $(PLATFORM),--platform $(PLATFORM)) -t "$(IMAGE)" .

image-multi:
	mkdir -p artifacts
	docker buildx build --platform "$(PLATFORMS)" --output type=oci,dest=artifacts/webcappi.oci.tar -t "$(IMAGE)" .

binaries:
	docker buildx build --target binary --platform "$(PLATFORMS)" --output type=local,dest=artifacts/binaries .

browser-test:
	bash scripts/browser-test.sh

redis-test:
	GO="$(GO)" bash scripts/redis-test.sh
