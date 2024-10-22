# Copyright 2024 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
from typing import List
from functools import wraps

import requests
from tenacity import retry, stop_after_attempt, wait_exponential_jitter

from .models import RunCodeRequest, RunCodeResponse, EvalResult, \
    GetPromptByIdRequest, GetPromptsRequest, Prompt, SubmitRequest, \
    CommandRunStatus, RunJupyterRequest, RunJupyterResponse, RunStatus, SummaryMapping

SANDBOX_ENDPOINT = os.environ.get('SANDBOX_FUSION_ENDPOINT', 'http://localhost:8000')

logger = logging.getLogger(__name__)


def set_endpoint(endpoint: str):
    global SANDBOX_ENDPOINT
    SANDBOX_ENDPOINT = endpoint


def on_retry_error(s):
    e = s.outcome.exception()
    logger.error(f'give up requesting sandbox. error: {e}')
    raise e


def before_retry_sleep(s):
    logger.warning(
        f'error requesting sandbox for {s.attempt_number} time(s), will retry... error: {s.outcome.exception()}')


def configurable_retry(max_attempts):

    def decorator(func):

        @wraps(func)
        @retry(wait=wait_exponential_jitter(),
               stop=stop_after_attempt(max_attempts),
               before_sleep=before_retry_sleep,
               retry_error_callback=on_retry_error)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        return wrapper

    return decorator


def run_code(request: RunCodeRequest, endpoint: str = '', max_attempts: int = 5) -> RunCodeResponse:

    @configurable_retry(max_attempts)
    def _run_code(request: RunCodeRequest) -> RunCodeResponse:
        result = requests.post(f'{endpoint or SANDBOX_ENDPOINT}/run_code', json=request.dict())
        if result.status_code != 200:
            raise Exception(f'Faas api responded with code {result.status_code}: {result.text}')
        resp = RunCodeResponse(**result.json())
        if resp.status == RunStatus.SandboxError:
            raise Exception(f'Sandbox responded with error: {resp.message}')
        return resp

    return _run_code(request)


def summary_run_code_result(result: RunCodeResponse, mapping: SummaryMapping) -> str:
    if result.compile_result is None and result.run_result is None:
        # note: this should not happen
        if result.status == RunStatus.Success:
            return mapping.Success
        if result.status == RunStatus.Failed:
            return mapping.Failed
        raise Exception(f'unexpected result status {result.status}')
    if result.run_result is None:
        # compile error
        if result.compile_result.status == CommandRunStatus.TimeLimitExceeded:
            return mapping.CompileTimeout or mapping.Failed
        return_code = result.compile_result.return_code
        if return_code is None:
            raise Exception(f'invalid sandbox result: no return code with status {result.compile_result.status}')
        if return_code != 0:
            return mapping.CompileFailed or mapping.Failed
        raise Exception(f'invalid sandbox result: compiled succesfully with no run result')
    if result.run_result.status == CommandRunStatus.TimeLimitExceeded:
        return mapping.RunTimeout or mapping.Failed
    return_code = result.run_result.return_code
    if return_code is None:
        raise Exception(f'invalid sandbox result: no return code with status {result.run_result.status}')
    if return_code != 0:
        return mapping.RunFailed or mapping.Failed
    return mapping.Success


def run_jupyter(request: RunJupyterRequest, endpoint: str = '', max_attempts: int = 3) -> RunJupyterResponse:

    @configurable_retry(max_attempts)
    def _run_jupyter(request: RunJupyterRequest) -> RunJupyterResponse:
        result = requests.post(f'{endpoint or SANDBOX_ENDPOINT}/run_jupyter', json=request.dict())
        if result.status_code != 200:
            raise Exception(f'Faas api responded with code {result.status_code}: {result.text}')
        resp = RunJupyterResponse(**result.json())
        if resp.status == RunStatus.SandboxError:
            raise Exception(f'Sandbox responded with error: {resp.message}')
        return resp

    return _run_jupyter(request)


def get_prompts(request: GetPromptsRequest, endpoint: str = '') -> List[Prompt]:
    result = requests.post(f'{endpoint or SANDBOX_ENDPOINT}/get_prompts', json=request.dict())
    if result.status_code != 200:
        raise Exception(f'Faas api responded with code {result.status_code}: {result.text}')
    resp = [Prompt(**r) for r in result.json()]
    return resp


def get_prompt_by_id(request: GetPromptByIdRequest, endpoint: str = '') -> Prompt:
    result = requests.post(f'{endpoint or SANDBOX_ENDPOINT}/get_prompt_by_id', json=request.dict())
    if result.status_code != 200:
        raise Exception(f'Faas api responded with code {result.status_code}: {result.text}')
    resp = Prompt(**result.json())
    return resp


def submit(request: SubmitRequest, endpoint: str = '', max_attempts: int = 5) -> EvalResult:

    @configurable_retry(max_attempts)
    def _submit(request: SubmitRequest) -> EvalResult:
        result = requests.post(f'{endpoint or SANDBOX_ENDPOINT}/submit', json=request.dict())
        if result.status_code != 200:
            raise Exception(f'Faas api responded with code {result.status_code}: {result.text}')
        resp = EvalResult(**result.json())
        return resp

    return _submit(request)


def submit_safe(request: SubmitRequest, endpoint: str = '', max_attempts: int = 5) -> EvalResult:
    try:
        return submit(request, endpoint, max_attempts)
    except Exception:
        logger.warning('failed to request sandbox, a rejected result is returned')
        return EvalResult(id=request.id, accepted=False, extracted_code='', tests=[])