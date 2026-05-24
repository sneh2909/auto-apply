"""Email-parser registry. Adding a new platform = add a parser here and import it."""

from autoapply.sources.base import EmailParser
from autoapply.sources.parsers.instahyre import InstahyreParser
from autoapply.sources.parsers.jobs2web import Jobs2WebParser
from autoapply.sources.parsers.linkedin import LinkedInParser
from autoapply.sources.parsers.naukri import NaukriParser


def all_parsers() -> list[EmailParser]:
    # Jobs2Web before LinkedIn — talent-community digests match broad subject patterns.
    return [
        Jobs2WebParser(),
        NaukriParser(),
        InstahyreParser(),
        LinkedInParser(),
    ]


__all__ = ["all_parsers"]
