GO ?= go
STATICCHECK ?= staticcheck
RUFF ?= ruff
HELM ?= $(CURDIR)/bin/chart-tools/helm
HELM_SMOKE_SANDBOX ?= enabled
IMAGE ?= webcappi:dev
PLATFORM ?=
PLATFORMS ?= linux/amd64,linux/arm64
WEBCAPPI_VERSION ?= $(shell python3 scripts/build_info.py version)
WEBCAPPI_REVISION ?= $(shell python3 scripts/build_info.py revision)
WEBCAPPI_MODIFIED ?= $(shell python3 scripts/build_info.py modified)
BUILD_PACKAGE = github.com/dayflower/webcappi/internal/buildinfo
BUILD_LDFLAGS = -X $(BUILD_PACKAGE).Version=$(WEBCAPPI_VERSION) -X $(BUILD_PACKAGE).Revision=$(WEBCAPPI_REVISION) -X $(BUILD_PACKAGE).Modified=$(WEBCAPPI_MODIFIED)
BUILD_ARGS = --build-arg WEBCAPPI_VERSION="$(WEBCAPPI_VERSION)" --build-arg WEBCAPPI_REVISION="$(WEBCAPPI_REVISION)" --build-arg WEBCAPPI_MODIFIED="$(WEBCAPPI_MODIFIED)"

.PHONY: build test image image-multi binaries browser-test redis-test
.PHONY: format-check format-check-go format-check-python format format-go format-python
.PHONY: lint lint-go lint-python
.PHONY: chart-tools chart-check chart-package helm-smoke helm-network-smoke
build:
	$(GO) build -trimpath -buildvcs=false -ldflags "$(BUILD_LDFLAGS)" -o bin/webcappi ./cmd/webcappi

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
	docker buildx build --load $(if $(PLATFORM),--platform $(PLATFORM)) $(BUILD_ARGS) -t "$(IMAGE)" .

image-multi:
	mkdir -p artifacts
	docker buildx build --platform "$(PLATFORMS)" $(BUILD_ARGS) --output type=oci,dest=artifacts/webcappi.oci.tar -t "$(IMAGE)" .

binaries:
	docker buildx build --target binary --platform "$(PLATFORMS)" $(BUILD_ARGS) --output type=local,dest=artifacts/binaries .

browser-test:
	bash scripts/browser-test.sh

redis-test:
	GO="$(GO)" bash scripts/redis-test.sh

chart-tools:
	python3 scripts/chart-tools.py

chart-check:
	GO="$(GO)" python3 scripts/chart-check.py --helm "$(HELM)"

# Packaging runs the same checks before exporting artifacts/charts/*.tgz.
chart-package: chart-check

helm-smoke:
	python3 scripts/helm-smoke.py --helm "$(HELM)" --image "$(IMAGE)" --sandbox "$(HELM_SMOKE_SANDBOX)"

helm-network-smoke:
	python3 scripts/helm-smoke.py --helm "$(HELM)" --image "$(IMAGE)" --sandbox enabled --network-only
