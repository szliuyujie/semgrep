#!/usr/bin/env python3
import argparse
import base64
import collections
import itertools
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Dict, Generator, List, Optional, Set, Tuple
from urllib.parse import urlparse
from datetime import datetime

import requests
import yaml

# TODO: support nested expressions under pattern-either


# Constants

REPO_HOME_DOCKER = "/home/repo/"
DEFAULT_CONFIG_FILE = ".sgrep.yml"
DEFAULT_CONFIG_FOLDER = ".sgrep"
DEFAULT_LANG = "python"

MISSING_RULE_ID = 'no-rule-id'

RULES_REGISTRY = {"r2c": "https://github.com/returntocorp/sgrep-rules/tarball/master"}
RULES_KEY = "rules"
ID_KEY = "id"


class OPERATORS:
    AND_NOT = "and_not"
    AND = "and"
    AND_EITHER = "and_either"
    AND_INSIDE = "and_inside"
    AND_NOT_INSIDE = "and_not_inside"


MUST_HAVE_KEYS = {"id", "message", "languages", "severity"}

PATTERN_NAMES_MAP = {
    "pattern-inside": OPERATORS.AND_INSIDE,
    "pattern-not-inside": OPERATORS.AND_NOT_INSIDE,
    "pattern-either": OPERATORS.AND_EITHER,
    "pattern-not": OPERATORS.AND_NOT,
    "pattern": OPERATORS.AND,
}

YML_EXTENSIONS = {".yml", ".yaml"}
DEBUG = False
QUIET = False
SGREP_PATH = "sgrep"

# helper functions


def is_url(url: str) -> bool:
    try:
        result = urlparse(url)
        return all([result.scheme, result.netloc])
    except ValueError:
        return False


def print_error(e):
    if not QUIET:
        print(str(e), file=sys.stderr)


def print_error_exit(msg: str, exit_code: int = 1) -> None:
    if not QUIET:
        print(msg, file=sys.stderr)
    sys.exit(exit_code)


def print_msg(msg: str):
    if not QUIET:
        print(msg, file=sys.stderr)


def debug_print(msg: str):
    if DEBUG:
        print(msg, file=sys.stderr)


def flatten(L: List[List[Any]]) -> List[Any]:
    for list in L:
        for item in list:
            yield item


# sgrep functions


def _parse_boolean_expression(rule_patterns, counter=0):
    for pattern in rule_patterns:
        for boolean_operator, pattern_text in pattern.items():
            if boolean_operator == "pattern-either":
                yield (
                    OPERATORS.AND_EITHER,
                    list(range(counter, counter + len(pattern_text))),
                )
                counter += len(pattern_text)
            else:
                yield (operator_for_pattern_name(boolean_operator), [counter])
                counter += 1


def build_boolean_expression(rule):
    """
    Build a (flat, not nested #TODO boolean expression from the yml lines in the rule)
    """

    if "pattern" in rule:  # single pattern
        yield (OPERATORS.AND, [0])
    elif "patterns" in rule:  # multiple patterns
        yield from _parse_boolean_expression(rule["patterns"])
    else:
        assert False


def operator_for_pattern_name(pattern_name):
    return PATTERN_NAMES_MAP[pattern_name]


def parse_rule_patterns(rule):
    if "pattern" in rule:  # single pattern
        yield (0, rule["pattern"])
    elif "patterns" in rule:  # multiple patterns
        yield from parse_pattern_expression(rule["patterns"])
    else:
        assert False


def parse_pattern_expression(rule_patterns, counter=0):
    #    print((counter, rule_patterns))
    for pattern in rule_patterns:
        for boolean_operator, pattern_text in pattern.items():
            if boolean_operator == "pattern-either":
                yield from parse_pattern_expression(pattern_text, counter)
                counter += len(pattern_text)
            else:
                yield (counter, pattern_text)
                counter += 1


@dataclass(frozen=True)
class Range:
    start: int
    end: int

    def is_enclosing_or_eq(self, other_range):
        return self.start <= other_range.start and other_range.end <= self.end

    def __repr__(self):
        return f"{self.start}-{self.end}"


def _evaluate_single_expression(
    operator, pattern_id, results, ranges_left: Set[Range]
) -> List[Range]:
    results_for_pattern = results.get(pattern_id, [])
    if operator == OPERATORS.AND:
        # remove all ranges that don't equal the ranges for this pattern
        return ranges_left.intersection(results_for_pattern)
    elif operator == OPERATORS.AND_NOT:
        # remove all ranges that DO equal the ranges for this pattern
        # difference_update = Remove all elements of another set from this set.
        return ranges_left.difference(results_for_pattern)
    elif operator == OPERATORS.AND_INSIDE:
        # remove all ranges (not enclosed by) or (not equal to) the inside ranges
        output_ranges = set()
        for arange in ranges_left:
            for keep_inside_this_range in results_for_pattern:
                is_enclosed = keep_inside_this_range.is_enclosing_or_eq(arange)
                # print(
                #    f'candidate range is {arange}, needs to be `{operator}` {keep_inside_this_range}; keep?: {keep}')
                if is_enclosed:
                    output_ranges.add(arange)
                    break  # found a match, no need to keep going
        # print(f"after filter `{operator}`: {output_ranges}")
        return output_ranges
    elif operator == OPERATORS.AND_NOT_INSIDE:
        # remove all ranges enclosed by or equal to
        output_ranges = ranges_left.copy()
        for arange in ranges_left:
            for keep_inside_this_range in results_for_pattern:
                if keep_inside_this_range.is_enclosing_or_eq(arange):
                    output_ranges.remove(arange)
                    break
        # print(f"after filter `{operator}`: {output_ranges}")
        return output_ranges
    else:
        assert False, f"unknown operator {operator}"


def evaluate_expression(expression, results: Dict[str, List[Range]]) -> List[Range]:
    ranges_left = set(flatten(results.values()))
    for (operator, pattern_ids) in expression:
        if operator == OPERATORS.AND_EITHER:
            # create a set from the union of the expressions in the `or` block
            either_ranges = set(flatten((results.get(pid, [])) for pid in pattern_ids))
            # remove anything that does not equal one of these ranges
            ranges_left.intersection_update(either_ranges)
            # print(f"after filter `{operator}`: {ranges_left}")
        else:
            assert (
                len(pattern_ids) == 1
            ), f"only {OPERATORS.AND_EITHER} expressions can have multiple pattern names"
            ranges_left = _evaluate_single_expression(
                operator, pattern_ids[0], results, ranges_left
            )
    return ranges_left


def parse_sgrep_output(sgrep_findings: List[Dict[str, Any]]) -> Dict[str, List[Range]]:
    output = collections.defaultdict(list)
    for finding in sgrep_findings:
        check_id = finding["check_id"]
        pattern_id = int(check_id.split(".")[1])
        output[pattern_id].append(sgrep_finding_to_range(finding))
    return dict(output)


def sgrep_finding_to_range(sgrep_finding: Dict[str, Any]):
    return Range(sgrep_finding["start"]["offset"], sgrep_finding["end"]["offset"])


def invoke_sgrep(
    all_rules: List[Dict[str, Any]], targets: List[Path]
) -> Dict[str, Any]:
    """Returns parsed json output of sgrep"""
    with tempfile.NamedTemporaryFile("w") as fout:
        # very important not to sort keys here
        yaml_as_str = yaml.safe_dump({"rules": all_rules}, sort_keys=False)
        debug_print(yaml_as_str)
        fout.write(yaml_as_str)
        fout.flush()
        cmd = [SGREP_PATH, f"-rules_file", fout.name, *[str(path) for path in targets]]
        output = subprocess.check_output(cmd, shell=False)
        output_json = json.loads((output.decode("utf-8")))
        return output_json


def rewrite_message_with_metavars(yaml_rule, sgrep_result):
    msg_text = yaml_rule["message"]
    if "metavars" in sgrep_result["extra"]:
        for metavar, contents in sgrep_result["extra"]["metavars"].items():
            msg_text = msg_text.replace(metavar, contents["abstract_content"])
    return msg_text


def transform_to_r2c_output(finding: Dict[str, Any]) -> Dict[str, Any]:
    # https://docs.r2c.dev/en/latest/api/output.html does not support offset at the moment
    if "offset" in finding["start"]:
        del finding["start"]["offset"]
    if "offset" in finding["end"]:
        del finding["end"]["offset"]
    return finding


def flatten_rule_patterns(all_rules):
    for rule_index, rule in enumerate(all_rules):
        patterns_with_ids = list(parse_rule_patterns(rule))
        for (pattern_index, pattern) in patterns_with_ids:
            # if we don't copy an array (like `languages`), the yaml file will refer to it by reference (with an anchor)
            # which is nice and all but the sgrep YAML parser doesn't support that
            new_check_id = f"{rule_index}.{pattern_index}"
            yield {
                "id": new_check_id,
                "pattern": pattern,
                "severity": rule["severity"],
                "languages": rule["languages"].copy(),
                "message": "<internalonly>",
            }


# CLI helper functions

def get_base_path() -> Path:
    docker_folder = Path(REPO_HOME_DOCKER)
    if docker_folder.exists():
        return docker_folder
    else:
        return Path(".")


def resolve_targets(targets: List[str]) -> List[Path]:
    base_path = get_base_path()
    return [
        Path(target) if Path(target).is_absolute() else base_path.joinpath(target)
        for target in targets
    ]


### Config helpers


def load_config_from_disk(loc: Path) -> Any:
    try:
        with loc.open() as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        print_error(f"YAML file at {loc} not found")
        return None
    except yaml.scanner.ScannerError as se:
        print_error(se)
        return None


def parse_config_string(config_id: str, contents: str) -> Dict[str, Any]:
    try:
        return {config_id: yaml.safe_load(contents)}
    except yaml.scanner.ScannerError as se:
        print_error(se)
        return {config_id: None}


def parse_config_file(loc: Path) -> Dict[str, Any]:
    config_id = str(loc)  # TODO
    return {config_id: load_config_from_disk(loc)}


def parse_config_folder(loc: Path) -> Dict[str, Any]:
    configs = {}
    for l in loc.rglob("*"):
        if l.suffix in YML_EXTENSIONS:
            config_id = str(l)  # TODO
            configs[config_id] = load_config_from_disk(l)
    return configs


def load_config(location: Optional[str] = None) -> Any:
    base_path = get_base_path()
    if location is None:
        default_file = base_path.joinpath(DEFAULT_CONFIG_FILE)
        default_folder = base_path.joinpath(DEFAULT_CONFIG_FOLDER)
        if default_file.exists():
            return parse_config_file(default_file)
        elif default_folder.exists():
            return parse_config_folder(default_folder)
        else:
            print_error_exit(f"unable to find a config file in {base_path.resolve()}")
    else:
        loc = base_path.joinpath(location)
        if loc.exists():
            if loc.is_file():
                return parse_config_file(loc)
            elif loc.is_dir():
                return parse_config_folder(loc)
            else:
                print_error_exit(f"{loc} is not a file or folder!")
        else:
            print_error_exit(f"unable to find a config file in {base_path.resolve()}")


def download_config(config_url: str) -> Any:
    print_error(f"trying to download from {config_url}")
    try:
        r = requests.get(config_url, stream=True)
        if r.status_code == requests.codes.ok:
            content_type = r.headers.get("Content-Type")
            if "text/plain" in content_type:
                return parse_config_string(config_url, r.content)
            elif content_type == "application/x-gzip":
                fname = f"/tmp/{base64.b64encode(config_url.encode()).decode()}"
                with tarfile.open(fileobj=r.raw, mode="r:gz") as tar:
                    tar.extractall(fname)
                extracted = Path(fname)
                for path in extracted.iterdir():
                    # get first folder in extracted folder (this is how GH does it)
                    return parse_config_folder(path)
            else:
                print_error_exit(f"unknown content-type: {content_type}. Can not parse")
    except Exception as e:
        print_error(e)
        return None


def resolve_config(config_str: Optional[str]) -> Any:
    """ resolves if config arg is a registry entry, a url, or a file, folder, or loads from defaults if None"""
    if config_str is None:
        config = load_config()
    elif config_str in RULES_REGISTRY:
        config = download_config(RULES_REGISTRY[config_str])
    elif is_url(config_str):
        config = download_config(config_str)
    else:
        config = load_config(config_str)
    return config


def validate_configs(configs: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """ Take configs and separate into valid and invalid ones"""

    # TODO: validate the rule patterns are ok by invoking sgrep core
    errors = {}
    valid = {}
    for config_id, config in configs.items():
        if not config:
            errors[config_id] = config
            continue
        if RULES_KEY not in config:
            print_error(f"{config_id} is missing `{RULES_KEY}` as top-level key")
            errors[config_id] = config
            continue
        rules = config.get(RULES_KEY, [])
        valid_rules = []
        invalid_rules = []
        for i, rule in enumerate(rules):
            if rule:
                rule_id_err_msg = f'(rule id: {rule.get("id", MISSING_RULE_ID)})'
                if not set(rule.keys()).issuperset(MUST_HAVE_KEYS):
                    print_error(
                        f"{config_id} is missing keys at rule {i+1} {rule_id_err_msg}, must have: {MUST_HAVE_KEYS}"
                    )
                    invalid_rules.append(rule)
                elif not "pattern" in rule and not "patterns" in rule:
                    print_error(
                        f"{config_id} is missing key `pattern` or `patterns` at rule {i+1} {rule_id_err_msg}"
                    )
                    invalid_rules.append(rule)
                elif "patterns" in rule and not rule["patterns"]:
                    print_error(
                        f"{config_id} no patterns found inside rule {i+1} {rule_id_err_msg}"
                    )
                    invalid_rules.append(rule)
                else:
                    valid_rules.append(rule)
        if invalid_rules:
            errors[config_id] = {**config, "rules": invalid_rules}
        if valid_rules:
            valid[config_id] = {**config, "rules": valid_rules}
    return valid, errors


def convert_config_id_to_prefix(config_id: str) -> str:
    return ".".join(PurePath(config_id).parts[:-1])


def rename_rule_ids(valid_configs: Dict[str, Any]) -> Dict[str, Any]:
    transformed = {}
    for config_id, config in valid_configs.items():
        rules = config.get(RULES_KEY, [])
        transformed_rules = [
            {
                **rule,
                ID_KEY: f"{convert_config_id_to_prefix(config_id)}.{rule.get(ID_KEY, MISSING_RULE_ID)}".lstrip(
                    "."
                ),
            }
            for rule in rules
        ]
        transformed[config_id] = {**config, RULES_KEY: transformed_rules}
    return transformed


def flatten_configs(transformed_configs: Dict[str, Any]) -> List[Any]:
    return [
        rule
        for config in transformed_configs.values()
        for rule in config.get(RULES_KEY, [])
    ]


def manual_config(pattern: str, lang: str) -> Dict[str, Any]:
    # TODO remove when using sgrep -e ... -l ... instead of this hacked config
    return {
        "manual": {
            RULES_KEY: [
                {
                    ID_KEY: "manual_id",
                    "pattern": pattern,
                    "message": "Manual Pattern",
                    "languages": [lang],
                    "severity": "ERROR",
                }
            ]
        }
    }


### Handle output


def post_output(output_url: str, output_data: Dict[str, Any]) -> None:
    print_msg(f"posting to {output_url}...")
    r = requests.post(output_url, json=output_data)
    debug_print(f"posted to {output_url} and got status_code:{r.status_code}")


def save_output(output_str: str, output_data: Dict[str, Any]):
    if is_url(output_str):
        post_output(output_str, output_data)
    else:
        if Path(output_str).is_absolute():
            save_path = Path(output_str)
        else:
            base_path = get_base_path()
            save_path = base_path.joinpath(output_str)

        with save_path.open() as fout:
            json.dump(output_data, fout)


def set_flags(debug: bool, quiet: bool) -> None:
    """Set the global DEBUG and QUIET flags"""
    # TODO move to a proper logging framework
    global DEBUG
    global QUIET
    if debug:
        DEBUG = True
        debug_print("DEBUG is on")
    if quiet:
        QUIET = True
        debug_print("QUIET is on")


# entry point
def main(args: argparse.Namespace):
    """ main function that parses args and runs sgrep """

    # set the flags
    set_flags(args.verbose, args.quiet)

    # get the proper paths for targets i.e. handle base path of /home/repo when it exists in docker
    targets = resolve_targets(args.target)

    # first let's check for a pattern
    if args.pattern:
        # and a language
        if args.lang:
            lang = args.lang
        else:
            lang = DEFAULT_LANG
        pattern = args.pattern

        # TODO for now we generate a manual config. Might want to just call sgrep -e ... -l ...
        configs = manual_config(pattern, lang)
    else:
        # else let's get a config. A config is a dict from config_id -> config. Config Id is not well defined at this point.
        configs = resolve_config(args.config)

    # if we can't find a config, bail
    if not configs:
        print_error_exit(f"unable to resolve {args.config}")

    # let's split our configs into valid and invalid configs.
    # It's possible that a config_id exists in both because we check valid rules and invalid rules
    # instead of just hard failing for that config if mal-formed
    valid_configs, errors = validate_configs(configs)

    validate = args.validate
    strict = args.strict

    if errors:
        if strict:
            print_error_exit(f"run with --strict and there were {len(errors)} errors")
        elif validate:
            print_error_exit(f"run with --validate and there were {len(errors)} errors")
    elif validate:  # no errors!
        print_error_exit("Config is valid", exit_code=0)

    if not args.no_rewrite_rule_ids:
        # re-write the configs to have the hierarchical rule ids
        valid_configs = rename_rule_ids(valid_configs)
        
    # extract just the rules from valid configs
    all_rules = flatten_configs(valid_configs)

    print_msg(
        f"running {len(all_rules)} rules from {len(valid_configs)} yaml files ({len(errors)} yaml files were invalid)"
    )
    # TODO log valid and invalid configs if verbose

    # a rule can have multiple patterns inside it. Flatten these so we can send sgrep a single yml file list of patterns
    all_patterns = list(flatten_rule_patterns(all_rules))

    # actually invoke sgrep
    start = datetime.now()
    output_json = invoke_sgrep(all_patterns, targets)
    debug_print(f"sgrep ran in {datetime.now() - start}")
    debug_print(output_json)

    # group output; we want to see all of the same rule ids on the same file path
    by_rule_index = collections.defaultdict(lambda: collections.defaultdict(list))
    for finding in output_json["matches"]:
        rule_index = int(finding["check_id"].split(".")[0])
        by_rule_index[rule_index][finding["path"]].append(finding)

    outputs_after_booleans = []
    for rule_index, paths in by_rule_index.items():
        expression = list(build_boolean_expression(all_rules[rule_index]))
        debug_print(f"rule expression: {expression}")
        for filepath, results in paths.items():
            debug_print(
                f"-------- rule (index {rule_index}) {all_rules[rule_index]['id']}------ filepath: {filepath}"
            )
            check_ids_to_ranges = parse_sgrep_output(results)
            debug_print(check_ids_to_ranges)
            valid_ranges_to_output = evaluate_expression(
                expression, check_ids_to_ranges
            )

            # only output matches which are inside these offsets!
            debug_print(f"compiled result {valid_ranges_to_output}")
            debug_print("-" * 80)
            for result in results:
                if sgrep_finding_to_range(result) in valid_ranges_to_output:
                    # restore the original rule ID
                    result["check_id"] = all_rules[rule_index]["id"]
                    # restore the original message
                    result["extra"]["message"] = rewrite_message_with_metavars(
                        all_rules[rule_index], result
                    )
                    result = transform_to_r2c_output(result)
                    outputs_after_booleans.append(result)

    # output results
    output_data = {"results": outputs_after_booleans}
    if not QUIET:
        print(json.dumps(output_data))
    if args.output:
        save_output(args.output, output_data)


# CLI

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="sgrep CLI. For more information about sgrep, go to https://sgrep.dev/",
        prog="sgrep",  # we have to lie to the user since they know of this as `sgrep`
    )

    # input
    parser.add_argument(
        "target",
        nargs="*",
        default=["."],
        help="Files to search (by default, entire current working directory searched). Implied argument if piping to sgrep.",
    )

    # config options
    config = parser.add_argument_group("config")
    config_ex = config.add_mutually_exclusive_group()

    config_ex.add_argument(
        "-f",
        "--config",
        help=f"Config YAML file or directory of YAML files ending in .yml|.yaml, OR URL of a config file, OR sgrep registry entry name. See the README for sgrep for information on config file format.",
    )

    config_ex.add_argument("-e", "--pattern", help="sgrep pattern")
    config.add_argument(
        "-l",
        "--lang",
        help="Parses pattern and all files in specified language. Must be used with -e/--pattern.",
    )
    config.add_argument(
        "--validate",
        help=f"Validate config file(s). No search is performed.",
        action="store_true",
    )
    config.add_argument(
        "--strict",
        help=f"only invoke sgrep if config(s) are valid",
        action="store_true",
    )

    config.add_argument(
        "--no-rewrite-rule-ids",
        help="Do not rewrite rule ids when they appear in nested subfolders (by default, rule 'foo' in test/rules.yaml will be renamed 'test.foo')",
        action="store_true",
    )

    # output options
    output = parser.add_argument_group("output")

    output.add_argument(
        "-q",
        "--quiet",
        help="Do not print anything to stdout. Search results can still be saved to an output file specified by -o/--output. Exit code provides success status.",
        action="store_true",
    )
    
    output.add_argument(
        "-o",
        "--output",
        help="Save search results to a file or post to URL. Default is to print to stdout.",
    )
    output.add_argument(
        "--json", help="Convert search output to JSON format.", action="store_true"
    )
    # logging options
    logging = parser.add_argument_group("logging")

    logging.add_argument(
        "-v",
        "--verbose",
        help=f"Sets the logging level to verbose. E.g. statements about which files are being processed will be printed.",
        action="store_true",
    )

    ### Parse and validate
    args = parser.parse_args()
    if args.lang and not args.pattern:
        parser.error("-e/--pattern is required when -l/--lang is used.")

    main(args)