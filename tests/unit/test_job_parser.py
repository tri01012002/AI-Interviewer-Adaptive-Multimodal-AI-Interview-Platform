from services.job_request_parser_service import find_missing, JobRequestParser
from apps.api.v1.schemas.job import JobRequestFromUser

def test_find_missing_reports_empty_fields():
    brief = JobRequestFromUser(title="AI Engineer", must_have_skills=[], location=None)
    assert find_missing(brief) == ["must_have_skills", "location", "years_of_experience", "job_type", "salary_range", "benefits"]



class FakeProvider:
    def generate(self, system_instruction, user_content):
        return ""

    def generate_structured(self, system_instruction, user_content, schema):
        return schema(title="AI Engineer", must_have_skills=["PyTorch"], location=None)


def test_parser_uses_provider_output():
    parser = JobRequestParser(provider=FakeProvider())
    brief = parser.parse("cần dev AI")
    assert find_missing(brief) == ["location", "years_of_experience", "job_type", "salary_range", "benefits"]


def test_additional_notes_is_not_required():
    brief = JobRequestFromUser(
        title="AI Engineer", must_have_skills=["PyTorch"], location="HCM",
        years_of_experience=1, job_type="full-time", salary_range="x", benefits=["y"],
        additional_notes=["Tiếng Anh giao tiếp tốt"],
    )
    assert find_missing(brief) == []
