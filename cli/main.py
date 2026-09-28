"""CLI composition entry point: route, configure, validate, then run the application."""

import subprocess
import sys

from llm import ConfigurationError, LLMError

from .application import run_application
from .arguments import parse_arguments, validate_execution_options
from .commands import dispatch_command, review_sandbox
from .startup import configuration_hint, prepare_model, startup_environment


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if dispatch_command(argv):
        return
    with startup_environment(argv):
        _main(argv)


def _main(argv=None) -> None:
    parser, args = parse_arguments(argv)
    if args.sandbox_review:
        review_sandbox(parser, args)
        return
    capabilities = validate_execution_options(parser, args)
    hint = configuration_hint(args)
    prepare_model(parser, args, hint)
    try:
        if not run_application(args, capabilities):
            parser.exit(1)
    except ConfigurationError as error:
        parser.exit(1, f"模型配置不完整或无效：{error}\n{hint}\n")
    except (LLMError, ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{type(error).__name__}: {error}\n")


if __name__ == "__main__":
    main()
