"""
The MIT License (MIT)
Copyright © 2015 Justin Ian Scott
See LICENSE
"""

import json
import random
import string

from contextlib import contextmanager
from html.parser import HTMLParser


class MLStripper(HTMLParser):
    """Used for stripping HTML from text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fed = []

    def handle_data(self, d):
        self.fed.append(d)

    def get_data(self):
        """Gets the non-HTML data from text that was fed in"""

        return ''.join(self.fed)


def html_to_text(html):
    """Strips HTML tags from given text and returns it."""

    stripper = MLStripper()
    stripper.feed(html)
    return stripper.get_data()


def random_id(length):
    """Generates a random ID of given length"""

    def char():
        """Generate single random char"""

        return random.choice(string.ascii_letters + string.digits)

    return ''.join(char() for _ in range(length))


def to_json(obj):
    """Create a compact JSON string from an object"""

    return json.dumps(obj, separators=(',', ':'))

@contextmanager
def delayed_close(closable):
    """Delay close until this contextmanager dies"""
    close = getattr(closable, "close", None)
    if close:
        # we do not want the library to close file in case we need to
        # resume, hence make close a no-op
        def replacement_close(*args, **kw):
            """ No op """
            pass
        setattr(closable, "close", replacement_close)
    try:
        yield closable
    finally:
        if close:
            setattr(closable, "close", close)
            closable.close()
