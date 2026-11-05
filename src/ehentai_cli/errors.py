class CLIError(Exception):
    """An actionable error whose message contains no credentials or response body."""

    code = "error"
    exit_code = 1


class InputError(CLIError):
    code = "invalid_input"
    exit_code = 2


class AuthError(CLIError):
    code = "authentication_failed"
    exit_code = 3


class NetworkError(CLIError):
    code = "network_error"
    exit_code = 4


class ParseError(CLIError):
    code = "unexpected_page"
    exit_code = 4


class RateLimitError(NetworkError):
    code = "rate_limited"


class ConflictError(CLIError):
    code = "state_conflict"


class UnknownResultError(CLIError):
    code = "result_unknown"
