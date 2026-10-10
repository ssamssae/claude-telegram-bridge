"""Include adjacent runtime resources in both wheel and source distributions."""

from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py


RUNTIME_RESOURCES = (
    "composer-clear.sh",
    "lib/interstitial-patterns.tsv",
    "hooks/claude-telegram-bridge-session-start.sh",
    "hooks/claude-telegram-bridge-pretool-block.sh",
    "hooks/claude-askq-sidecar.sh",
)


class BuildWithRuntimeResources(build_py):
    def run(self):
        super().run()
        root = Path(__file__).resolve().parent
        for relative in RUNTIME_RESOURCES:
            target = Path(self.build_lib) / relative
            self.mkpath(str(target.parent))
            self.copy_file(str(root / relative), str(target))

    def get_source_files(self):
        return super().get_source_files() + list(RUNTIME_RESOURCES)

    def get_outputs(self, include_bytecode=True):
        return super().get_outputs(include_bytecode) + [
            str(Path(self.build_lib) / relative) for relative in RUNTIME_RESOURCES
        ]


setup(cmdclass={"build_py": BuildWithRuntimeResources})
