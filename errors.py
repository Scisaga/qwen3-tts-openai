class ServiceError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details=None):
        super().__init__(message)
        self.code, self.message, self.status, self.details = code, message, status, details

    def payload(self):
        return {"error": {"type": self.code, "message": self.message, "details": self.details}}


class Cancelled(Exception):
    pass
