"""Entry point for `python -m factura`.

Routes to subcommands:
  python -m factura setup-cert    → certificate setup
  python -m factura <proyecto> ... → invoice emission
"""
import sys

if len(sys.argv) > 1 and sys.argv[1] == "setup-cert":
    # Remove "setup-cert" from argv so argparse in setup_cert doesn't see it
    sys.argv = [sys.argv[0]] + sys.argv[2:]
    from factura.setup_cert import main
    main()
else:
    from factura.cli import main
    main()
