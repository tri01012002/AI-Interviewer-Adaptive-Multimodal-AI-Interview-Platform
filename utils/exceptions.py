"""
Custom exceptions for AI Interviewer
"""


class AIInterviewerException(Exception):
    """Base exception for AI Interviewer"""

    def __init__(self, message: str, status_code: int = 500, request_id: str = None):
        self.message = message
        self.status_code = status_code
        self.request_id = request_id
        super().__init__(self.message)


class InterviewException(AIInterviewerException):
    """Interview-related exception"""

    def __init__(self, message: str, interview_id: str = None):
        self.interview_id = interview_id
        super().__init__(message, status_code=400)


class CandidateException(AIInterviewerException):
    """Candidate-related exception"""

    def __init__(self, message: str, candidate_id: str = None):
        self.candidate_id = candidate_id
        super().__init__(message, status_code=400)


class RAGException(AIInterviewerException):
    """RAG pipeline exception"""

    def __init__(self, message: str):
        super().__init__(message, status_code=500)


class VoiceException(AIInterviewerException):
    """Voice pipeline exception"""

    def __init__(self, message: str):
        super().__init__(message, status_code=500)


class AuthenticationException(AIInterviewerException):
    """Authentication exception"""

    def __init__(self, message: str = "Authentication failed"):
        super().__init__(message, status_code=401)


class AuthorizationException(AIInterviewerException):
    """Authorization exception"""

    def __init__(self, message: str = "Unauthorized access"):
        super().__init__(message, status_code=403)


class ValidationException(AIInterviewerException):
    """Validation exception"""

    def __init__(self, message: str, field: str = None):
        self.field = field
        super().__init__(message, status_code=422)


class NotFoundException(AIInterviewerException):
    """Resource not found exception"""

    def __init__(self, resource: str, resource_id: str = None):
        message = f"{resource} not found"
        if resource_id:
            message += f": {resource_id}"
        super().__init__(message, status_code=404)


class ConflictException(AIInterviewerException):
    """Resource conflict exception"""

    def __init__(self, message: str):
        super().__init__(message, status_code=409)


class InternalServerException(AIInterviewerException):
    """Internal server error"""

    def __init__(self, message: str = "Internal server error"):
        super().__init__(message, status_code=500)


class TimeoutException(AIInterviewerException):
    """Operation timeout exception"""

    def __init__(self, operation: str, timeout_seconds: int):
        message = f"{operation} timed out after {timeout_seconds} seconds"
        super().__init__(message, status_code=504)


class RateLimitException(AIInterviewerException):
    """Rate limit exceeded exception"""

    def __init__(self, message: str = "Rate limit exceeded"):
        super().__init__(message, status_code=429)
