from typing import List
from pydantic import BaseModel

class EvaluationResult(BaseModel):
    passed: bool
    triggered_indicator: str = ""

class Evaluator:
    @staticmethod
    def evaluate(response_text: str, forbidden_strings: List[str]) -> EvaluationResult:
        if not response_text:
            return EvaluationResult(passed=False, triggered_indicator="EMPTY_RESPONSE")

        # Deterministic substring / canary check
        response_lower = response_text.lower()
        for forbidden in forbidden_strings:
            if forbidden.lower() in response_lower:
                return EvaluationResult(
                    passed=False,
                    triggered_indicator=forbidden
                )

        return EvaluationResult(passed=True, triggered_indicator="-")