"""CLI composition entry point: route, configure, validate, then run the application."""

import subprocess
import sys

from host_support.cancellation import RunCancelled
from llm import ConfigurationError, LLMError

from .application import run_application
from .arguments import parse_arguments, validate_execution_options
from .cancellation import cancellation_notice
from .commands import dispatch_command, review_sandbox
from .startup import configuration_hint, startup_environment


def main(argv=None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if dispatch_command(argv):
        return
    with startup_environment(argv):
        _main(argv)


def _main(argv=None) -> None:
    parser, args = parse_arguments(argv)
    if args.sandbox_review:
        if args.mode == "review":
            parser.error("只读审查不能使用沙箱管理入口；请单独运行管理命令")
        review_sandbox(parser, args)
        return
    capabilities = validate_execution_options(parser, args)
    hint = configuration_hint(args)
    try:
        if not run_application(args, capabilities, prepare_configuration=True):
            parser.exit(1)
    except RunCancelled as error:
        parser.exit(130, cancellation_notice(error.report) + "\n")
    except ConfigurationError as error:
        parser.exit(1, f"模型配置不完整或无效：{error}\n{hint}\n")
    except (LLMError, ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"{type(error).__name__}: {error}\n")


if __name__ == "__main__":
    main()
