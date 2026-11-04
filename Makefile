.PHONY: test run demo
test:
	python tools/check.py
run:
	python archiver/archive.py
demo:
	python tools/demo.py
