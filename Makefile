.PHONY: help install test lint fix demo serve ui docker clean build bundle bundle-full verify-wheel release-check

PY := ./.venv/bin/python
PIP := ./.venv/bin/pip
VERSION := $(shell $(PY) -c "import tomllib;print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])" 2>/dev/null || echo 0.0.0)
PYTHON_VERSION := $(shell $(PY) -c "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYTHON_BIN := python$(PYTHON_VERSION)
PYTHON_TUPLE := $(shell $(PY) -c "import sys;print(f'({sys.version_info.major}, {sys.version_info.minor})')")
BUNDLE_TAG := py$(shell $(PY) -c "import sys;print(f'{sys.version_info.major}{sys.version_info.minor}')")-$(shell $(PY) -c "import platform;print(platform.system().lower() + '-' + platform.machine().lower())")

help:
	@echo "install        create .venv and install with dev extras"
	@echo "test           run the test suite"
	@echo "lint           ruff check"
	@echo "fix            ruff check --fix"
	@echo "demo           review a seeded repo containing deliberate defects"
	@echo "serve          start the API on 127.0.0.1:8000"
	@echo "ui             start the Streamlit UI on 127.0.0.1:8501"
	@echo ""
	@echo "build          build the wheel and sdist into dist/"
	@echo "verify-wheel   install the built wheel in a clean venv and run it"
	@echo "bundle         offline install bundle, CLI only (small)"
	@echo "bundle-full    offline install bundle, including the UI (large)"
	@echo "release-check  everything that must pass before publishing"
	@echo "docker         build the container image"

install:
	python3 -m venv .venv
	$(PIP) install -q --upgrade pip
	$(PIP) install -e ".[ui,dev,analysers]"
	@echo "done. activate with: source .venv/bin/activate"

test:
	$(PY) -m pytest tests/ -q

lint:
	$(PY) -m ruff check src/ tests/

fix:
	$(PY) -m ruff check --fix src/ tests/

# Builds a throwaway repository whose HEAD commit contains a hardcoded secret,
# a SQL injection, a swallowed exception, assertion-free tests and dead
# parameters, then reviews it. Useful for eyeballing prompt or report changes.
demo:
	@rm -rf /tmp/vibekill-demo && mkdir -p /tmp/vibekill-demo/app /tmp/vibekill-demo/tests
	@cd /tmp/vibekill-demo && git init -q && \
		git config user.email demo@example.com && git config user.name Demo && \
		printf 'def retry(fn, attempts=3):\n    for _ in range(attempts):\n        try:\n            return fn()\n        except Exception:\n            continue\n    raise RuntimeError("failed")\n' > app/retry.py && \
		git add -A && git commit -qm initial
	@printf 'import sqlite3\n\nDUMMY_TOKEN = "sk_live_4f8a7b6c5d4e3f2a"\n\n\ndef find_user(name, locale="en", include_deleted=False):\n    conn = sqlite3.connect("users.db")\n    cur = conn.cursor()\n    query = "SELECT * FROM users WHERE name = \x27" + name + "\x27"\n    cur.execute(query)\n    return cur.fetchall()\n\n\ndef load(uid):\n    try:\n        return find_user(str(uid))\n    except Exception:\n        pass\n\n\ndef retry_call(fn, attempts=3, backoff=1, jitter=False, logger=None):\n    for _ in range(attempts):\n        try:\n            return fn()\n        except Exception:\n            continue\n    raise RuntimeError("failed")\n' > /tmp/vibekill-demo/app/users.py
	@printf 'from app.users import find_user\n\n\ndef test_find_user():\n    assert find_user("a") is not None\n\n\ndef test_placeholder():\n    assert True\n' > /tmp/vibekill-demo/tests/test_users.py
	@cd /tmp/vibekill-demo && git add -A && git commit -qm "Add user lookup"
	@# vibekill exits 1 when it requests changes, which the demo repo always does.
	@# That is the point of the demo, so it must not fail the make target.
	-PATH="$(PWD)/.venv/bin:$$PATH" $(PY) -m vibekill.cli review --repo /tmp/vibekill-demo --base HEAD~1 --provider none
	@echo ""
	@echo "HTML report: /tmp/vibekill-demo/.vibekill/review.html"

serve:
	$(PY) -m vibekill.cli serve

ui:
	$(PY) -m vibekill.cli ui

docker:
	docker build -t vibekill:$(VERSION) -t vibekill:latest .

# --------------------------------------------------------------------------- #
# Packaging
# --------------------------------------------------------------------------- #

build:
	rm -rf dist build
	$(PIP) install -q --upgrade build
	$(PY) -m build
	@echo ""
	@ls -lh dist/
	@echo ""
	@echo "Checking the template shipped inside the wheel..."
	@$(PY) -c "import zipfile,glob,sys; \
		w=glob.glob('dist/*.whl')[0]; \
		names=zipfile.ZipFile(w).namelist(); \
		t='vibekill/report/template.html'; \
		print('  ' + ('OK  ' if t in names else 'MISSING ') + t); \
		sys.exit(0 if t in names else 1)"

# Installs the built wheel into a throwaway venv and actually runs a review with
# it. Catches the packaging bugs that a source-tree test suite cannot see, such
# as a data file left out of the wheel or a missing console script.
verify-wheel: build
	rm -rf /tmp/vibekill-wheeltest
	python3 -m venv /tmp/vibekill-wheeltest
	/tmp/vibekill-wheeltest/bin/pip install -q dist/*.whl
	@echo ""
	@echo "installed version: $$(/tmp/vibekill-wheeltest/bin/vibekill version)"
	@echo "short alias:       $$(/tmp/vibekill-wheeltest/bin/vk version)"
	@rm -rf /tmp/vibekill-wheelrepo && mkdir -p /tmp/vibekill-wheelrepo
	@cd /tmp/vibekill-wheelrepo && git init -q && \
		git config user.email t@example.com && git config user.name T && \
		echo "x = 1" > a.py && git add -A && git commit -qm init && \
		printf 'import os\nDUMMY_TOKEN = "sk_live_abcdef123456"\n\n\ndef run(cmd):\n    os.system("sh -c " + cmd)\n' > a.py && \
		git add -A && git commit -qm bad
	/tmp/vibekill-wheeltest/bin/vibekill review --repo /tmp/vibekill-wheelrepo \
		--base HEAD~1 --provider none --format html,json -q || true
	@echo ""
	@$(PY) -c "import json; d=json.load(open('/tmp/vibekill-wheelrepo/.vibekill/review.json')); \
		print('findings from installed wheel:', len(d['findings'])); \
		print('html report rendered:', __import__('pathlib').Path('/tmp/vibekill-wheelrepo/.vibekill/review.html').stat().st_size, 'bytes')"
	@rm -rf /tmp/vibekill-wheeltest /tmp/vibekill-wheelrepo
	@echo ""
	@echo "wheel verified end to end"

# Offline install bundle for air-gapped machines.
#
# A bare wheel is not enough: pip still resolves dependencies from the network.
# This collects vibekill plus every transitive dependency as wheels, so the
# target machine can install with --no-index and never touch PyPI.
bundle: build
	rm -rf build/wheelhouse dist/vibekill-$(VERSION)-offline-$(BUNDLE_TAG).tar.gz
	mkdir -p build/wheelhouse
	$(PIP) download -q -d build/wheelhouse dist/vibekill-$(VERSION)-py3-none-any.whl
	cp dist/vibekill-$(VERSION)-py3-none-any.whl build/wheelhouse/
	@$(MAKE) --no-print-directory _bundle_pack SUFFIX=offline-$(BUNDLE_TAG)
	@echo ""
	@echo "CLI-only bundle. For the Streamlit UI too, use 'make bundle-full'."

# Same, with the UI extra. Considerably larger: streamlit pulls pandas, pyarrow
# and numpy, which is most of the download.
bundle-full: build
	rm -rf build/wheelhouse dist/vibekill-$(VERSION)-offline-full-$(BUNDLE_TAG).tar.gz
	mkdir -p build/wheelhouse
	$(PIP) download -q -d build/wheelhouse "$$(ls dist/vibekill-$(VERSION)-py3-none-any.whl)[ui]"
	cp dist/vibekill-$(VERSION)-py3-none-any.whl build/wheelhouse/
	@$(MAKE) --no-print-directory _bundle_pack SUFFIX=offline-full-$(BUNDLE_TAG)

# Assembles the archive. The installer and README live in packaging/ as real
# files so this target and the release workflow ship identical ones.
_bundle_pack:
	@sed \
		-e 's/__VERSION__/$(VERSION)/g' \
		-e 's/__PYTHON__/$(PYTHON_BIN)/g' \
		-e 's/__PYTHON_VERSION__/$(PYTHON_VERSION)/g' \
		-e 's/__PYTHON_TUPLE__/$(PYTHON_TUPLE)/g' \
		-e 's/__BUNDLE_TAG__/$(BUNDLE_TAG)/g' \
		packaging/install.sh > build/wheelhouse/install.sh
	@chmod +x build/wheelhouse/install.sh
	@sed \
		-e 's/__VERSION__/$(VERSION)/g' \
		-e 's/__PYTHON_VERSION__/$(PYTHON_VERSION)/g' \
		-e 's/__BUNDLE_TAG__/$(BUNDLE_TAG)/g' \
		packaging/BUNDLE_README.txt > build/wheelhouse/README.txt
	@tar -czf dist/vibekill-$(VERSION)-$(SUFFIX).tar.gz -C build/wheelhouse .
	@echo ""
	@echo "$$(ls -1 build/wheelhouse/*.whl | wc -l) wheels bundled"
	@ls -lh dist/vibekill-$(VERSION)-$(SUFFIX).tar.gz

release-check: lint test verify-wheel
	@echo ""
	@echo "ready to publish $(VERSION)"

clean:
	rm -rf .pytest_cache .ruff_cache dist build .vibekill
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
