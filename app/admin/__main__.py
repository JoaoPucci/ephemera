"""Entry point for `python -m app.admin <command> ...`.

The classic-script `if __name__ == "__main__": main()` shape doesn't
work for a package, so this dedicated dunder-main file forwards to
cli.main(). See app.admin.cli for the dispatch logic.
"""

from .cli import main

main()
