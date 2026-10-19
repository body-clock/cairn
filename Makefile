.PHONY: test up down ui ask

test:
	python3 -m unittest discover -s tests -v

up:
	./bin/kn up

down:
	./bin/kn down

ui:
	./bin/kui

ask:
	./bin/kn ask "$(Q)"
