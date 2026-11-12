"""Token-budgeted structured requests to the active llama-server."""

from functools import lru_cache
import json
import time
from urllib.request import Request, urlopen

from norev.core.ports import LlmOutputLimit


class BudgetedLlama:
    def __init__(self, url, *, model=None, timeout=600, report=None):
        self.url = url.rstrip('/')
        self.model, self.timeout, self.report = model, timeout, report
        settings = self.request('/props')['default_generation_settings']
        self.context = settings['n_ctx']
        self.limit = self.context * 65 // 100
        self.calls = []
        self.first_answer = None
        self.tokens = lru_cache(maxsize=128)(self._tokens)

    def request(self, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.url + path, data, {'Content-Type': 'application/json'})
        with urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    def _tokens(self, prompt):
        rendered = self.request('/apply-template', {'messages': self.messages(prompt)})['prompt']
        return len(self.request('/tokenize', {'content': rendered, 'add_special': True,
                                               'parse_special': True})['tokens'])

    @staticmethod
    def messages(prompt):
        return [{'role': 'user', 'content': prompt}]

    def generate_json(self, prompt, *, model=None, json_schema=None):
        count = self.tokens(prompt)
        if count > self.limit:
            raise ValueError(f'Prompt uses {count} tokens, above the {self.limit}-token limit')
        format_ = {'type': 'json_object'}
        if json_schema is not None:
            format_ = {'type': 'json_schema', 'json_schema': {'name': 'response', 'schema': json_schema}}
        started = time.monotonic()
        response = self.request('/v1/chat/completions', {
            'model': self.model or model, 'messages': self.messages(prompt), 'temperature': 0, 'seed': 42,
            'max_tokens': min(2800, self.context - count - 32), 'response_format': format_,
        })
        choice = response['choices'][0]
        usage = response['usage']
        if usage['prompt_tokens'] > self.limit or usage['prompt_tokens'] != count:
            raise RuntimeError(f'Server prompt token count differs: {usage["prompt_tokens"]} versus {count}')
        self.calls.append({**usage, 'seconds': round(time.monotonic() - started, 2),
                           'finish_reason': choice['finish_reason']})
        if self.report:
            self.report(self.calls[-1])
        if choice['finish_reason'] == 'length':
            raise LlmOutputLimit('Model reached the response token limit')
        if choice['finish_reason'] != 'stop':
            raise RuntimeError(f'Incomplete response: {choice["finish_reason"]}')
        answer = choice['message']['content']
        if self.first_answer is None:
            self.first_answer = answer
        return answer
