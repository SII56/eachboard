"""Structured application errors shared by CLI and storage."""
class EachboardError(Exception):

    def __init__(self, message, code='invalid_request'):
        super().__init__(message)
        self.code = code

def fail(message, code='invalid_request'):
    raise EachboardError(message, code)
