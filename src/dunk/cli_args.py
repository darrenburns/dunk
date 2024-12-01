from dataclasses import dataclass


@dataclass
class CliArgs:
    paths: list[str]
    left_path: str
    right_path: str
    base_path: bool
