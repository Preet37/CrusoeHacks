"""Small shared limits for one M2 moment's specialist tree."""


class JobBudget:
    def __init__(self, max_jobs: int = 12, max_tokens: int = 4000):
        self.max_jobs = max_jobs
        self.max_tokens = max_tokens
        self.jobs = 0
        self.tokens_reserved = 0

    def reserve_job(self) -> bool:
        if self.jobs >= self.max_jobs:
            return False
        self.jobs += 1
        return True

    def reserve_model(self, input_text: str, max_output_tokens: int) -> int:
        """Reserve an upper bound for input and output before making a call.

        Character count is a conservative, dependency-free token estimate for
        the small evidence windows used by the M2 specialists.
        """
        estimated_input = (len(input_text) + 2) // 3
        needed = estimated_input + max_output_tokens
        if needed > self.max_tokens - self.tokens_reserved:
            return 0
        self.tokens_reserved += needed
        return max_output_tokens
