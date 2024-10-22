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

import base64
import json
import pickle
import re
import zlib
from typing import Any, Dict, List

from sandbox.database import get_row_by_id_in_table, get_rows_in_table
from sandbox.datasets.types import (
    CodingDataset,
    EvalResult,
    EvalTestCase,
    GetPromptByIdRequest,
    GetPromptsRequest,
    Prompt,
    RunCodeRequest,
    RunStatus,
    SubmitRequest,
    TestConfig,
)
from sandbox.utils.extraction import default_extract_helper
from sandbox.utils.sandbox_client import run_code_in_sandbox

# Note: adapted from https://github.com/LiveCodeBench/LiveCodeBench
TEST_UTIL = r'''
import ast
import json
import sys
import faulthandler
import platform

# used for debugging to time steps
from datetime import datetime

# to run the solution files we're using a timing based approach
import signal

import numpy as np

# for capturing the stdout
from io import StringIO

# used for testing the code that reads from input
from unittest.mock import patch, mock_open

#from pyext import RuntimeModule

from enum import Enum

import types


class RuntimeModule:
    @staticmethod
    def from_string(name, _, code):
        module = types.ModuleType(name)
        exec(code, module.__dict__)
        return module


def truncatefn(s, length=300):
    assert isinstance(s, str)
    if len(s) <= length:
        return s

    return s[: length // 2] + "...(truncated) ..." + s[-length // 2 :]


class CODE_TYPE(Enum):
    call_based = 0
    standard_input = 1


# stuff for setting up signal timer
class TimeoutException(Exception):
    pass


def timeout_handler(signum, frame):
    print("alarm went off")
    # return
    raise TimeoutException


signal.signal(signal.SIGALRM, timeout_handler)
# timeout = 6  # seconds


# used to capture stdout as a list
# from https://stackoverflow.com/a/16571630/6416660
# alternative use redirect_stdout() from contextlib
class Capturing(list):
    def __enter__(self):
        self._stdout = sys.stdout
        sys.stdout = self._stringio = StringIO()
        # Make closing the StringIO a no-op
        self._stringio.close = lambda x: 1
        return self

    def __exit__(self, *args):
        self.append(self._stringio.getvalue())
        del self._stringio  # free up some memory
        sys.stdout = self._stdout


def only_int_check(val):
    return isinstance(val, int)


def string_int_check(val):
    return isinstance(val, str) and val.isdigit()


def combined_int_check(val):
    return only_int_check(val) or string_int_check(val)


def run_test(sample, test=None, debug=False, timeout=6):
    """
    if test(generated_code) is not None it'll try to run the code.
    otherwise it'll just return an input and output pair.
    """
    # Disable functionalities that can make destructive changes to the test.
    reliability_guard()

    if debug:
        print(f"start = {datetime.now().time()}")

    try:
        in_outs = json.loads(sample["input_output"])
    except ValueError:
        in_outs = None
    if in_outs:
        if in_outs.get("fn_name") is None:
            which_type = CODE_TYPE.standard_input  # Standard input
            method_name = None
        else:
            which_type = CODE_TYPE.call_based  # Call-based
            method_name = in_outs["fn_name"]

    if debug:
        print(f"loaded input_output = {datetime.now().time()}")

    if test is None:
        assert False, "should not happen: test code is none"
        return in_outs, {"error": "no test code provided"}
    elif test is not None:
        results = []
        sol = "from string import *\nfrom re import *\nfrom datetime import *\nfrom collections import *\nfrom heapq import *\nfrom bisect import *\nfrom copy import *\nfrom math import *\nfrom random import *\nfrom statistics import *\nfrom itertools import *\nfrom functools import *\nfrom operator import *\nfrom io import *\nfrom sys import *\nfrom json import *\nfrom builtins import *\nfrom typing import *\nimport string\nimport re\nimport datetime\nimport collections\nimport heapq\nimport bisect\nimport copy\nimport math\nimport random\nimport statistics\nimport itertools\nimport functools\nimport operator\nimport io\nimport sys\nimport json\nsys.setrecursionlimit(6*10**5)\n"
        if debug:
            print(f"loading test code = {datetime.now().time()}")

        if which_type == CODE_TYPE.call_based:

            sol += test
            if debug:
                print(f"sol = {sol}")
            signal.alarm(timeout)
            try:
                tmp_sol = RuntimeModule.from_string("tmp_sol", "", sol)
                if "class Solution" not in test:
                    tmp = tmp_sol
                else:
                    tmp = tmp_sol.Solution()
                signal.alarm(0)
            except Exception as e:
                signal.alarm(0)
                if debug:
                    print(f"type 0 compilation error = {e}")
                results.append(-2)
                return results, {
                    "error": repr(e),
                    "error_code": -1,
                    "error_message": "Compilation Error",
                }
            signal.alarm(0)

        elif which_type == CODE_TYPE.standard_input:
            # sol
            # if code has if __name__ == "__main__": then remove it
            try:
                astree = ast.parse(test)
                last_block = astree.body[-1]
                if isinstance(last_block, ast.If):
                    condition = last_block.test
                    if ast.unparse(condition).strip() == "__name__ == '__main__'":
                        test = (
                            ast.unparse(astree.body[:-1])
                            + "\n"
                            + ast.unparse(last_block.body)
                        )
            except:
                pass

            tmp_test = test.split("\n")

            new_test = []
            for x in tmp_test:
                if (not x.startswith("from ")) and (not x.startswith("import ")):
                    new_test.append("\t" + x + "\n")
                else:
                    new_test.append(x + "\n")
            tmp_test = new_test

            new_test = ""
            started = False
            for i in tmp_test:
                if i.startswith("\t") and not started:
                    new_test += "stdin = sys.stdin\nstdout = sys.stdout\n"
                    new_test += "def code():\n"
                    new_test += i
                    started = True
                elif started and ((i.startswith("from ")) or (i.startswith("import "))):
                    new_test += "\t" + i
                else:
                    new_test += i
            tmp_test = new_test

            sol += tmp_test
            if debug:
                print(f"sol = {sol}")
            method_name = "code"
            signal.alarm(timeout)
            try:
                tmp_sol = RuntimeModule.from_string("tmp_sol", "", sol)
                tmp = tmp_sol
                signal.alarm(0)
            except Exception as e:
                signal.alarm(0)
                if debug:
                    print(f"type 1 compilation error = {e}")
                results.append(-2)
                return results, {
                    "error": repr(e),
                    "error_code": -1,
                    "error_message": "Compilation Error",
                }
            signal.alarm(0)
        if debug:
            print(f"get method = {datetime.now().time()}")

        try:
            method = getattr(tmp, method_name)  # get_attr second arg must be str
        except:
            signal.alarm(0)
            e = sys.exc_info()
            print(f"unable to get function error = {e}")
            results.append(-2)
            return results, {
                "error": repr(e),
                "error_code": -1,
                "error_message": "Unable to extract code",
            }

        for index, inputs in enumerate(in_outs["inputs"]):
            raw_inputs = inputs
            raw_outputs = in_outs["outputs"][index]
            if which_type == CODE_TYPE.call_based:
                inputs = [json.loads(line) for line in inputs.split("\n")]
                in_outs["outputs"][index] = json.loads(in_outs["outputs"][index])

                truncate_line_size = 300 // (raw_inputs.count("\n") + 1)
                raw_inputs = "\n".join(
                    [
                        truncatefn(line, truncate_line_size)
                        for line in raw_inputs.strip().split("\n")
                    ]
                )
                raw_outputs = truncatefn(raw_outputs, 200)
            else:
                raw_inputs = truncatefn(raw_inputs)
                raw_outputs = truncatefn(raw_outputs, 200)
            # JSON forces dictionaries to have string keys; this undoes this (assuming a singleton list)
            try:
                if isinstance(inputs[0], dict):
                    inputs = [{int(k): v for k, v in inputs[0].items()}]
            except:
                True
            try:
                if isinstance(in_outs["outputs"][index], dict):
                    in_outs["outputs"][index] = [
                        {int(k): v for k, v in in_outs["outputs"][index].items()}
                    ]
            except:
                True
            try:
                if isinstance(in_outs["outputs"][index][0], dict):
                    in_outs["outputs"][index] = [
                        {int(k): v for k, v in in_outs["outputs"][index][0].items()}
                    ]
            except:
                True

            if debug:
                print(
                    f"time: {datetime.now().time()} testing index = {index}  inputs = {inputs}, {type(inputs)}. type = {which_type}"
                )
            if which_type == CODE_TYPE.call_based:  # Call-based
                signal.alarm(timeout)
                faulthandler.enable()
                try:
                    output = method(*inputs)
                    raw_true_output = output

                    raw_true_output_copy = json.dumps(output)
                    raw_true_output_copy = truncatefn(raw_true_output_copy, 200)

                    # ground truth sequences are not tuples
                    if isinstance(output, tuple):
                        output = list(output)

                    tmp_result = output == in_outs["outputs"][index]
                    if (
                        isinstance(in_outs["outputs"][index], list)
                        and in_outs["outputs"][index]
                    ):
                        tmp_result = tmp_result or (
                            output == in_outs["outputs"][index][0]
                        )

                    # ground truth sequences are not tuples
                    try:
                        if isinstance(output[0], tuple):
                            tmp_result = tmp_result or (
                                [list(x) for x in output]
                                == in_outs["outputs"][index][0]
                            )
                    except:
                        True
                    results.append(tmp_result)
                    if tmp_result != True:
                        return results, {
                            "output": raw_true_output_copy,
                            "expected": raw_outputs,
                            "inputs": raw_inputs,
                            "error_code": -2,
                            "error_message": "Wrong Answer",
                        }
                    # reset the alarm
                    signal.alarm(0)
                except Exception as e:
                    signal.alarm(0)
                    faulthandler.disable()
                    if debug:
                        print(
                            f"Standard input runtime error or time limit exceeded error = {e}"
                        )
                    results.append(-1)
                    if "timeoutexception" in repr(e).lower():
                        return results, {
                            "error": repr(e),
                            "error_code": -3,
                            "error_message": "Time Limit Exceeded",
                            "inputs": raw_inputs,
                            "expected": raw_outputs,
                        }
                    else:
                        return results, {
                            "error": repr(e),
                            "error_code": -4,
                            "error_message": "Runtime Error",
                            "inputs": raw_inputs,
                            "expected": raw_outputs,
                        }
                faulthandler.disable()
                signal.alarm(0)
                if debug:
                    print(
                        f"outputs = {output}, test outputs = {in_outs['outputs'][index]}, inputs = {inputs}, {type(inputs)}, {output == [in_outs['outputs'][index]]}"
                    )
            elif which_type == CODE_TYPE.standard_input:  # Standard input
                faulthandler.enable()
                passed = False

                if isinstance(inputs, list):
                    inputs = "\n".join(inputs)
                if isinstance(in_outs["outputs"][index], list):
                    in_outs["outputs"][index] = "\n".join(in_outs["outputs"][index])

                signal.alarm(timeout)
                with Capturing() as output:
                    try:
                        call_method(method, inputs)
                        # reset the alarm
                        signal.alarm(0)
                        passed = True
                    except Exception as e:
                        # runtime error or took too long
                        signal.alarm(0)
                        print(
                            f"Call-based runtime error or time limit exceeded error = {repr(e)}{e}"
                        )
                        results.append(-1)
                        if "timeoutexception" in repr(e).lower():
                            return results, {
                                "error": repr(e),
                                "error_code": -3,
                                "error_message": "Time Limit Exceeded",
                                "inputs": raw_inputs,
                                "expected": raw_outputs,
                            }
                        else:
                            return results, {
                                "error": repr(e),
                                "error_code": -4,
                                "error_message": "Runtime Error",
                                "inputs": raw_inputs,
                                "expected": raw_outputs,
                            }
                    signal.alarm(0)
                raw_true_output = output[0]
                raw_true_output_copy = truncatefn(raw_true_output, 200)
                output = raw_true_output.splitlines()
                if not passed:
                    if debug:
                        nl = "\n"
                        if not isinstance(inputs, list):
                            print(
                                f"not passed output = {output}, test outputs = {in_outs['outputs'][index]}, inputs = {inputs.replace(nl,' new-line ')}, {type(inputs)}, {output == [in_outs['outputs'][index]]}"
                            )
                        else:
                            print(
                                f"not passed output = {output}, test outputs = {in_outs['outputs'][index]}, inputs = {inputs}, {type(inputs)}, {output == [in_outs['outputs'][index]]}"
                            )
                    continue

                if passed and debug:
                    print(
                        f"==> output = {output}, test outputs = {in_outs['outputs'][index]}"
                    )

                if custom_compare_(output, in_outs["outputs"][index]):
                    tmp_result = True
                    results.append(tmp_result)
                    continue

                # ground truth sequences are expressed as lists not tuples
                if isinstance(output, tuple):
                    output = list(output)

                tmp_result = False
                try:
                    tmp_result = output == [in_outs["outputs"][index]]
                    if isinstance(in_outs["outputs"][index], list):
                        tmp_result = tmp_result or (output == in_outs["outputs"][index])
                        if isinstance(output[0], str):
                            tmp_result = tmp_result or (
                                [e.strip() for e in output] == in_outs["outputs"][index]
                            )
                except Exception as e:
                    if debug:
                        print(f"Failed check1 exception = {e}")
                    pass

                if tmp_result == True:
                    results.append(tmp_result)
                    continue

                # try one more time without \n
                if isinstance(in_outs["outputs"][index], list):
                    for tmp_index, i in enumerate(in_outs["outputs"][index]):
                        in_outs["outputs"][index][tmp_index] = i.split("\n")
                        in_outs["outputs"][index][tmp_index] = [
                            x.strip() for x in in_outs["outputs"][index][tmp_index] if x
                        ]
                else:
                    in_outs["outputs"][index] = in_outs["outputs"][index].split("\n")
                    in_outs["outputs"][index] = list(
                        filter(len, in_outs["outputs"][index])
                    )
                    in_outs["outputs"][index] = list(
                        map(lambda x: x.strip(), in_outs["outputs"][index])
                    )

                try:
                    tmp_result = output == [in_outs["outputs"][index]]
                    if isinstance(in_outs["outputs"][index], list):
                        tmp_result = tmp_result or (output == in_outs["outputs"][index])
                except Exception as e:
                    if debug:
                        print(f"Failed check2 exception = {e}")
                    pass

                if tmp_result == True:
                    results.append(tmp_result)
                    continue

                # try by converting the output into a split up list too
                if isinstance(output, list):
                    output = list(filter(len, output))

                if debug:
                    nl = "\n"
                    if not isinstance(inputs, list):
                        print(
                            f"@1 output = {output}, test outputs = {in_outs['outputs'][index]}, inputs = {inputs.replace(nl,' new-line ')}, {type(inputs)}, {output == [in_outs['outputs'][index]]} {tmp_result=}"
                        )
                    else:
                        print(
                            f"@1 output = {output}, test outputs = {in_outs['outputs'][index]}, inputs = {inputs}, {type(inputs)}, {output == [in_outs['outputs'][index]]} {tmp_result=}"
                        )

                if tmp_result == True:
                    results.append(tmp_result)
                    continue

                if debug:
                    print(f"{tmp_result=} @a")

                try:
                    tmp_result = output == [in_outs["outputs"][index]]
                    if isinstance(in_outs["outputs"][index], list):
                        tmp_result = tmp_result or (output == in_outs["outputs"][index])
                except Exception as e:
                    if debug:
                        print(f"Failed check3 exception = {e}")
                    pass

                if debug:
                    print(f"{tmp_result=} @b")

                try:
                    all_ints = all(
                        combined_int_check(e1) and combined_int_check(e2)
                        for e1, e2 in zip(output, in_outs["outputs"][index])
                    )
                    if not all_ints:
                        if debug:
                            print(
                                [
                                    combined_int_check(e1) and combined_int_check(e2)
                                    for e1, e2 in zip(output, in_outs["outputs"][index])
                                ]
                            )
                        output_float = [float(e) for e in output]
                        gt_float = [float(e) for e in in_outs["outputs"][index]]
                        tmp_result = tmp_result or (
                            (len(output_float) == len(gt_float))
                            and np.allclose(output_float, gt_float)
                        )
                except Exception as e:
                    pass

                if debug:
                    print(f"{tmp_result=} @c")

                try:
                    if isinstance(output[0], list):
                        all_ints = all(
                            combined_int_check(e1) and combined_int_check(e2)
                            for e1, e2 in zip(output[0], in_outs["outputs"][index])
                        )
                        if not all_ints:
                            output_float = [float(e) for e in output[0]]
                            gt_float = [float(e) for e in in_outs["outputs"][index][0]]
                            tmp_result = tmp_result or (
                                (len(output_float) == len(gt_float))
                                and np.allclose(output_float, gt_float)
                            )
                except Exception as e:
                    pass

                if tmp_result == True:
                    results.append(tmp_result)
                    continue

                if debug:
                    print(f"{tmp_result=} @d")
                # try by converting the stuff into split up list
                if isinstance(in_outs["outputs"][index], list):
                    for tmp_index, i in enumerate(in_outs["outputs"][index]):
                        in_outs["outputs"][index][tmp_index] = set(i.split())
                else:
                    in_outs["outputs"][index] = set(in_outs["outputs"][index].split())

                if debug:
                    print(f"{tmp_result=} @e")

                try:
                    tmp_result = output == in_outs["outputs"][index]
                except Exception as e:
                    if debug:
                        print(f"Failed check4 exception = {e}")
                    continue

                if tmp_result == True:
                    results.append(tmp_result)
                    continue

                if debug:
                    print(f"{tmp_result=} @f")

                # try by converting the output into a split up list too
                if isinstance(output, list):
                    for tmp_index, i in enumerate(output):
                        output[tmp_index] = i.split()
                    output = list(filter(len, output))
                    for tmp_index, i in enumerate(output):
                        output[tmp_index] = set(i)
                else:
                    output = output.split()
                    output = list(filter(len, output))
                    output = set(output)

                if debug:
                    print(f"{tmp_result=} @g")
                # try:
                #     tmp_result = set(frozenset(s) for s in output) == set(
                #         frozenset(s) for s in in_outs["outputs"][index]
                #     )
                # except Exception as e:
                #     if debug:
                #         print(f"Failed check5 exception = {e}")

                # if they are all numbers, round so that similar numbers are treated as identical
                # try:
                #     all_ints = all(
                #         combined_int_check(e1) and combined_int_check(e2)
                #         for e1, e2 in zip(output, in_outs["outputs"][index])
                #     )
                #     tmp_result = tmp_result or (
                #         set(frozenset(round(float(t), 3) for t in s) for s in output)
                #         == set(
                #             frozenset(round(float(t), 3) for t in s)
                #             for s in in_outs["outputs"][index]
                #         )
                #     )
                # except Exception as e:
                #     if debug:
                #         print(f"Failed check6 exception = {e}")

                if debug:
                    print(f"{tmp_result=} @h")

                if tmp_result == True and debug:
                    print("PASSED")

                results.append(tmp_result)
                if tmp_result != True:
                    return results, {
                        "output": raw_true_output_copy,
                        "expected": raw_outputs,
                        "inputs": raw_inputs,
                        "error_code": -2,
                        "error_message": "Wrong Answer",
                    }

                if debug:
                    nl = "\n"
                    if not isinstance(inputs, list):
                        print(
                            f"@2 output = {output}, test outputs = {in_outs['outputs'][index]}, inputs = {inputs.replace(nl,' new-line ')}, {type(inputs)}, {output == [in_outs['outputs'][index]]}"
                        )
                    else:
                        print(
                            f"@2 output = {output}, test outputs = {in_outs['outputs'][index]}, inputs = {inputs}, {type(inputs)}, {output == [in_outs['outputs'][index]]}"
                        )

                    print(f"results = {results}")

    return results, {}


def custom_compare_(output, ground_truth):

    if isinstance(output, list):
        output_1 = "\n".join(output)
        if stripped_string_compare(output_1, ground_truth):
            return True

    if isinstance(output, list):
        output_2 = [o.lstrip().rstrip() for o in output]
        output_2 = "\n".join(output_2)
        if stripped_string_compare(output_2, ground_truth):
            return True

    return False


def stripped_string_compare(s1, s2):
    s1 = s1.lstrip().rstrip()
    s2 = s2.lstrip().rstrip()
    return s1 == s2


def call_method(method, inputs):

    if isinstance(inputs, list):
        inputs = "\n".join(inputs)

    inputs_line_iterator = iter(inputs.split("\n"))

    # sys.setrecursionlimit(10000)

    # @patch('builtins.input', side_effect=inputs.split("\n"))
    @patch("builtins.open", mock_open(read_data=inputs))
    @patch("sys.stdin", StringIO(inputs))
    @patch("sys.stdin.readline", lambda *args: next(inputs_line_iterator))
    @patch("sys.stdin.readlines", lambda *args: inputs.split("\n"))
    @patch("sys.stdin.read", lambda *args: inputs)
    # @patch('sys.stdout.write', print)
    def _inner_call_method(_method):
        try:
            return _method()
        except SystemExit as e:
            pass
        finally:
            pass

    return _inner_call_method(method)


def reliability_guard(maximum_memory_bytes=None):
    """
    This disables various destructive functions and prevents the generated code
    from interfering with the test (e.g. fork bomb, killing other processes,
    removing filesystem files, etc.)
    WARNING
    This function is NOT a security sandbox. Untrusted code, including, model-
    generated code, should not be blindly executed outside of one. See the
    Codex paper for more information about OpenAI's code sandbox, and proceed
    with caution.
    """

    if maximum_memory_bytes is not None:
        import resource

        resource.setrlimit(
            resource.RLIMIT_AS, (maximum_memory_bytes, maximum_memory_bytes)
        )
        resource.setrlimit(
            resource.RLIMIT_DATA, (maximum_memory_bytes, maximum_memory_bytes)
        )
        if not platform.uname().system == "Darwin":
            resource.setrlimit(
                resource.RLIMIT_STACK, (maximum_memory_bytes, maximum_memory_bytes)
            )

    faulthandler.disable()

    import builtins

    builtins.exit = None
    builtins.quit = None

    import os

    os.environ["OMP_NUM_THREADS"] = "1"

    os.kill = None
    os.system = None
    os.putenv = None
    os.remove = None
    os.removedirs = None
    os.rmdir = None
    os.fchdir = None
    os.setuid = None
    os.fork = None
    os.forkpty = None
    os.killpg = None
    os.rename = None
    os.renames = None
    os.truncate = None
    os.replace = None
    os.unlink = None
    os.fchmod = None
    os.fchown = None
    os.chmod = None
    os.chown = None
    os.chroot = None
    os.fchdir = None
    os.lchflags = None
    os.lchmod = None
    os.lchown = None
    os.getcwd = None
    os.chdir = None

    import shutil

    shutil.rmtree = None
    shutil.move = None
    shutil.chown = None

    import subprocess

    subprocess.Popen = None  # type: ignore

    __builtins__["help"] = None

    import sys

    sys.modules["ipdb"] = None
    sys.modules["joblib"] = None
    sys.modules["resource"] = None
    sys.modules["psutil"] = None
    sys.modules["tkinter"] = None
'''
TEST_CODE = '''
import json
import sys

from testing_util import run_test

with open('test_cases.txt', 'r') as fin:
    sample = json.load(fin)
with open('code.py', 'r') as fin:
    code = fin.read()

result, metadata = run_test(sample, code, debug=False, timeout=%(timeout)s)
if any(x != True for x in result):
    print(metadata)
    sys.exit(-1)
else:
    sys.exit(0)
'''

FEWSHOT_PREFIX = """### Instruction
You are an expert Python programmer. You will be given a question (problem specification) and will generate a correct Python program that matches the specification and passes all tests. You will NOT return anything except for the program.

### Question:
Calculate a+b

Input

Two integer a,b (0<=a,b<=10)

Output

Output a+b

Sample Input

1 2

Sample Output

3

### Format: Read the inputs from stdin solve the problem and write the answer to stdout (do not directly test on the sample inputs). Enclose your code within delimiters as follows.
```python
```

### Answer: (use the provided format with backticks)
```python
a,b = map(int, input().split())
print(a+b)
```

### Question:
Given an array of integers nums and an integer target, return indices of the two numbers such that they add up to target.

You may assume that each input would have exactly one solution, and you may not use the same element twice.

You can return the answer in any order.



Example 1:

Input: nums = [2,7,11,15], target = 9
Output: [0,1]
Explanation: Because nums[0] + nums[1] == 9, we return [0, 1].
Example 2:

Input: nums = [3,2,4], target = 6
Output: [1,2]
Example 3:

Input: nums = [3,3], target = 6
Output: [0,1]

Constraints:

2 <= nums.length <= 10^4
-10^9 <= nums[i] <= 10^9
-10^9 <= target <= 10^9
Only one valid answer exists.

### Format: You will use the following starter code to write the solution to the problem and enclose your code within delimiters.
```python
class Solution:
    def twoSum(self, nums: List[int], target: int) -> List[int]:

```

### Answer: (use the provided format with backticks)
```python
class Solution:
    def twoSum(self, nums: List[int], target: int) -> List[int]:
        for i in range(len(nums)):
            for j in range(i + 1, len(nums)):
                if nums[i] + nums[j] == target:
                    return [i, j]
```

### Question:
Larry graduated this year and finally has a job. He's making a lot of money, but somehow never seems to have enough. Larry has decided that he needs to grab hold of his financial portfolio and solve his financing problems. The first step is to figure out what's been going on with his money. Larry has his bank account statements and wants to see how much money he has. Help Larry by writing a program to take his closing balance from each of the past twelve months and calculate his average account balance.
Input

The input will be twelve lines. Each line will contain the closing balance of his bank account for a particular month. Each number will be positive and displayed to the penny. No dollar sign will be included.
Output

The output will be a single number, the average (mean) of the closing balances for the twelve months. It will be rounded to the nearest penny, preceded immediately by a dollar sign, and followed by the end-of-line. There will be no other spaces or characters in the output.
Sample Input

100.00
489.12
12454.12
1234.10
823.05
109.20
5.27
1542.25
839.18
83.99
1295.01
1.75
Sample Output

$1581.42

### Format: Read the inputs from stdin solve the problem and write the answer to stdout (do not directly test on the sample inputs). Enclose your code within delimiters as follows.
```python
```

### Answer: (use the provided format with backticks)
```python
sum = 0
for i in range(12):
    sum += float(input())
print("$%.2f" % (sum / 12))
```

### Question:
"""


def extract_question(prompt: str):
    question = re.search(r'### Question:\n(.*)\n\n### Format:', prompt, re.DOTALL)
    start_code = re.search(r'### Format: .*\n```python\n(.*)\n```\n\n', prompt, re.DOTALL)
    assert question is not None and start_code is not None

    question = question.group(1)
    start_code = start_code.group(1)
    if start_code == '# YOUR CODE HERE':
        start_code = None
    return question, start_code


def generate_fewshot_prompt(question: str, starter_code: str) -> str:
    prompt = FEWSHOT_PREFIX + question + '\n\n'
    if starter_code:
        prompt += f"### Format: You will use the following starter code to write the solution to the problem and enclose your code within delimiters.\n```python\n{starter_code}\n```"
    else:
        prompt += "### Format: Read the inputs from stdin solve the problem and write the answer to stdout (do not directly test on the sample inputs). Enclose your code within delimiters as follows.\n```python\n```"
    prompt += "\n\n### Answer: (use the provided format with backticks)\n"
    return prompt


def _b64encode(content: str) -> str:
    return base64.b64encode(content.encode('utf-8')).decode('utf-8')


class LiveCodeBenchDataset(CodingDataset, dataset_ids=['live_code_bench_v1']):
    table_names = {
        'live_code_bench_v1': 'code_eval_live_code_bench_v1',
    }

    @classmethod
    async def get_num_problems(cls, dataset_id: str) -> str:
        return {'live_code_bench_v1': 511}[dataset_id]

    @classmethod
    async def get_prompts(cls, request: GetPromptsRequest) -> List[Prompt]:
        rows = await get_rows_in_table(
            request,
            cls.get_table_name(request.dataset),
            columns=['id', 'labels', 'content'],
        )
        return [cls._generate_single_prompt(r, request.config) for r in rows]

    @classmethod
    async def get_prompt_by_id(cls, request: GetPromptByIdRequest) -> Prompt:
        row = await get_row_by_id_in_table(
            request,
            cls.get_table_name(request.dataset),
            columns=['id', 'labels', 'content'],
        )
        return cls._generate_single_prompt(row, request.config)

    @classmethod
    def _generate_fewshot_prompt(cls, prompt: str) -> str:
        question, start_code = extract_question(prompt)
        fewshot_prompt = generate_fewshot_prompt(question, start_code)
        return fewshot_prompt

    @classmethod
    def _generate_single_prompt(cls, row: Dict[str, Any], config: TestConfig) -> Prompt:
        prompt = row['content']
        if config.is_fewshot:
            prompt = cls._generate_fewshot_prompt(prompt)
        return Prompt(id=row['id'], prompt=prompt, labels=json.loads(row['labels']))

    @classmethod
    async def evaluate_single(cls, request: SubmitRequest) -> EvalResult:
        row = await get_row_by_id_in_table(
            request,
            cls.get_table_name(request.dataset),
            columns=['id', 'labels', 'content', 'test'],
        )
        labels = json.loads(row['labels'])
        timeout = request.config.run_timeout or 6
        try:
            test_cases = json.loads(row['test'])
        except:
            test_cases = pickle.loads(zlib.decompress(base64.b64decode(row['test'])))
        test_cnt = len(json.loads(test_cases['input_output'])['inputs'])
        total_timeout = min((timeout + 1) * test_cnt + 5, 315)

        code = default_extract_helper(request.completion, 'python', request.config.custom_extract_logic)
        test_code = TEST_CODE % {'timeout': timeout}
        asset = {
            'test_cases.txt': _b64encode(json.dumps(test_cases)),
            'code.py': _b64encode(code),
            'testing_util.py': _b64encode(TEST_UTIL),
        }

        result = await run_code_in_sandbox(
            RunCodeRequest(
                code=test_code,
                language='python',
                run_timeout=total_timeout,
                files=asset,
            ))
        accepted = result.status == RunStatus.Success

        return EvalResult(
            id=request.id,
            accepted=accepted,
            extracted_code=code,
            full_code=code,
            test_code=test_code,
            tests=[EvalTestCase(passed=accepted, exec_info=result)],
        )