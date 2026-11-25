"""Stable, non-sensitive public errors shared by HTTP and execution layers."""


class ServiceError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def error_response(error):
    return {"status": "failed", "error": {"code": error.code, "message": error.message},
            "content": error.message, "mdData": "", "chartData": {},
            "sql": "", "chartType": "", "extra": ""}
