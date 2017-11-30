test:
	PYTHONPATH=. py.test -v tests/
check:
	PYTHONPATH=. flake8 .
