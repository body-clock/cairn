.PHONY: test up down ui ask

test:
	python3 -m unittest discover -s tests -v

up:
	./bin/cairn up

down:
	./bin/cairn down

ui:
	./bin/cairn-ui

ask:
	./bin/cairn ask "$(Q)"
