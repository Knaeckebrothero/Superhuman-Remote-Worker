"""Value-free, structured errors for authored configuration."""

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class ManifestIssue:
    code: str
    message: str
    document: int = 1
    path: str = "/"


class ManifestError(ValueError):
    def __init__(self, issue: ManifestIssue):
        self.issue = issue
        super().__init__(issue.message)

    def as_dict(self) -> dict:
        return asdict(self.issue)


def fail(code: str, message: str, *, document: int = 1, path: str = "/"):
    raise ManifestError(ManifestIssue(code, message, document, path))


def pointer(parts) -> str:
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)
