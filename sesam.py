import requests
import argparse
import logging
import threading
import shutil
import logging.handlers
import time
import sys
import os
import os.path
import io
import copy
import glob
from lxml import etree
import sesamclient
import configparser
import itertools
import json
import os
import zipfile
import uuid
from difflib import unified_diff
from fnmatch import fnmatch
from decimal import Decimal
import pprint

sesam_version = "1.15.10"

logger = logging.getLogger('sesam')
LOGLEVEL_TRACE = 2


class SesamParser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write('error: %s\n\n' % message)
        self.print_help()
        sys.exit(2)


class TestSpec:
    """ Test specification """

    def __init__(self, filename):
        self._spec = {}
        self._spec_file = filename

        self._spec["name"] = filename[:-len(".test.json")]
        self._spec["file"] = self.name + ".json"
        self._spec["endpoint"] = "json"

        if self.name.find("/") > -1:
            self._spec["pipe"] = self.name.split("/")[-1]
        else:
            self._spec["pipe"] = self.name

        with open(filename, "r", encoding="utf-8-sig") as fp:
            spec_dict = json.load(fp)
            if isinstance(spec_dict, dict):
                self._spec.update(spec_dict)
            else:
                logger.error("Test spec '%s' not in correct json format" % filename)
                raise AssertionError("Test spec not a json object")

    @property
    def spec(self):
        return self._spec

    @property
    def spec_file(self):
        return self._spec_file

    @property
    def file(self):
        return self._spec.get("file")

    @property
    def name(self):
        return self._spec.get("name")

    @property
    def endpoint(self):
        return self._spec.get("endpoint")

    @property
    def pipe(self):
        return self._spec.get("pipe")

    @property
    def stage(self):
        return self._spec.get("stage")

    @property
    def blacklist(self):
        return self._spec.get("blacklist")

    @property
    def id(self):
        return self._spec.get("_id")

    @property
    def ignore(self):
        return self._spec.get("ignore", False) is True

    @property
    def parameters(self):
        return self.spec.get("parameters")

    @property
    def expected_data(self):
        filename = self.file
        if not filename.startswith("expected/"):
            filename = "expected/" + filename

        with open(filename, "rb") as fp:
            return fp.read()

    @property
    def expected_entities(self):
        filename = self.file
        if not filename.startswith("expected/"):
            filename = "expected/" + filename

        with open(filename, "r", encoding="utf-8-sig") as fp:
            return json.load(fp)

    def update_expected_data(self, data):
        filename = self.file
        if not filename.startswith("expected/"):
            filename = "expected/" + filename

        if os.path.isfile(filename) is False:
            logger.debug("Creating new expected data file '%s'" % filename)

        with open(filename, "wb") as fp:
            fp.write(data)

    def is_path_blacklisted(self, path):
        blacklist = self.blacklist
        if blacklist and isinstance(blacklist, list):
            prop_path = ".".join(path).replace("\.", ".")

            for pattern in blacklist:
                if fnmatch(prop_path, pattern.replace("[].", ".*.")):
                    return True

        return False


class SesamNode:
    """ Sesam node functions wrapped in a class to facilitate unit tests """

    def __init__(self, node_url, jwt_token, logger, verify_ssl=True):
        self.logger = logger

        self.node_url = node_url
        self.jwt_token = jwt_token

        self.logger.debug("Connecting to Seasam using url '%s' and JWT token '%s'", node_url, jwt_token)

        self.api_connection = sesamclient.Connection(sesamapi_base_url=self.node_url, jwt_auth_token=self.jwt_token,
                                                     timeout=60 * 10, verify_ssl=verify_ssl)

    def put_config(self, config, force=False):
        self.logger.log(LOGLEVEL_TRACE, "PUT config to %s" % self.node_url)
        self.api_connection.upload_config(config, force=force)

    def put_env(self, env_vars):
        self.logger.log(LOGLEVEL_TRACE, "PUT env vars to %s" % self.node_url)
        self.api_connection.put_env_vars(env_vars)

    def get_system(self, system_id):
        self.logger.log(LOGLEVEL_TRACE, "Get system '%s' from %s" % (system_id, self.node_url))
        return self.api_connection.get_system(system_id)

    def add_system(self, config, verify=False, timeout=300):
        self.logger.log(LOGLEVEL_TRACE, "Add system '%s' to %s" % (config, self.node_url))

        self.api_connection.add_systems([config])

        if not verify:
            return True

        # If verify is set, we wait until the runtime config matches the given config
        self.logger.debug("Verifying posted system '%s'.." % config["_id"])
        sleep_time = 5.0
        while timeout > 0:
            try:
                system = self.get_system(config["_id"])
                # Check if config now matches the config we posted
                if system.config["original"] == config:
                    self.logger.debug("Posted system '%s' verified OK!" % config["_id"])
                    return True
            except BaseException as e:
                pass

            time.sleep(sleep_time)

            timeout -= sleep_time

        self.logger.debug("Failed to verify posted system '%s'!" % config["_id"])
        return False

    def add_systems(self, config):
        self.logger.log(LOGLEVEL_TRACE, "Add systems '%s' to %s" % (config, self.node_url))
        return self.api_connection.add_systems(config)

    def remove_system(self, system_id):
        self.logger.log(LOGLEVEL_TRACE, "Remove system '%s' from %s" % (system_id, self.node_url))
        try:
            system = self.api_connection.get_system(system_id)
            if system is not None:
                system.delete()
        except:
            logger.warning("Could not remove system '%s' - perhaps it doesn't exist" % system_id)

    def get_config(self, binary=False):
        data = self.api_connection.get_config_as_zip()
        if not binary:
            return zipfile.ZipFile(io.BytesIO(data))

        return data

    def remove_all_datasets(self):
        self.logger.log(LOGLEVEL_TRACE, "Remove alle datasets from %s" % self.node_url)
        for dataset in self.api_connection.get_datasets():
            dataset_id = dataset.id
            if not dataset.id.startswith("system:"):
                try:
                    dataset.delete()
                    self.logger.debug("Dataset '%s' deleted" % dataset_id)
                except BaseException as e:
                    self.logger.error("Failed to delete dataset '%s'" % dataset.id)
                    raise e

    def get_pipe_type(self, pipe):
        source_config = pipe.config["effective"].get("source",{})
        sink_config = pipe.config["effective"].get("sink",{})
        source_type = source_config.get("type", "")
        sink_type = sink_config.get("type", "")

        if source_type == "embedded":
            return "input"

        if isinstance(sink_type, str) and sink_type.endswith("_endpoint"):
            return "endpoint"

        if (source_config.get("dataset") or source_config.get("datasets")) and\
                sink_config.get("dataset"):
            return "internal"

        if not sink_config.get("dataset"):
            return "output"

        return "internal"

    def get_output_pipes(self):
        return [p for p in self.api_connection.get_pipes() if self.get_pipe_type(p) == "output"]

    def get_input_pipes(self):
        return [p for p in self.api_connection.get_pipes() if self.get_pipe_type(p) == "input"]

    def get_endpoint_pipes(self):
        return [p for p in self.api_connection.get_pipes() if self.get_pipe_type(p) == "endpoint"]

    def get_internal_pipes(self):
        return [p for p in self.api_connection.get_pipes() if self.get_pipe_type(p) == "internal"]

    def run_internal_scheduler(self, disable_pipes=True):
        internal_scheduler_url = "%s/pipes/run-all-pipes" % self.node_url

        if disable_pipes:
            params = {"disable_pipes": "true"}
        else:
            params = None

        resp = self.api_connection.session.post(internal_scheduler_url, params=params)
        resp.raise_for_status()

        return resp.json()

    def get_internal_scheduler_log(self, since=None):
        scheduler_log_url  = "%s/pipes/get-run-all-pipes-log" % self.node_url

        if since:
            params = {"since": since}
        else:
            params = None

        resp = self.api_connection.session.get(scheduler_log_url, params=params)
        resp.raise_for_status()

        return resp.json()

    def get_pipe_entities(self, pipe, stage=None):
        if stage is None:
            pipe_url = "%s/pipes/%s/entities" % (self.node_url, pipe.id)
        else:
            pipe_url = "%s/pipes/%s/entities?stage=%s" % (self.node_url, pipe.id, stage)

        resp = self.api_connection.session.get(pipe_url)
        resp.raise_for_status()

        return resp.json()

    def get_published_data(self, pipe, type="entities", params=None, binary=False):

        pipe_url = "%s/publishers/%s/%s" % (self.node_url, pipe.id, type)

        # Enable the pump, if it is disabled, or else we can't get the data
        pump = pipe.get_pump()
        if pump.is_disabled:
            pump.enable()

        resp = self.api_connection.session.get(pipe_url, params=params)
        resp.raise_for_status()

        if binary:
            return resp.content

        return resp.text

    def get_system_status(self, system_id):
        system_url = self.api_connection.get_system_url(system_id)

        resp = self.api_connection.session.get(system_url + "/status")
        resp.raise_for_status()

        status = resp.json()
        if isinstance(status, dict):
            return status

        return None

    def get_system_log(self, system_id, params=None):

        system_url = self.api_connection.get_system_url(system_id)

        resp = self.api_connection.session.get("%s/logs" % system_url, params=params)
        resp.raise_for_status()

        return resp.text

    def wait_for_microservice(self, microservice_id, timeout=300):
        """ Polls the microservice status API until it is running (or we time out) """

        system = self.get_system(microservice_id)
        if system is None:
            raise AssertionError("Microservice system '%s' doesn't exist" % microservice_id)

        sleep_time = 5.0
        while timeout > 0:
            try:
                system_status = self.get_system_status(microservice_id)
            except BaseException as e:
                self.logger.debug("Failed to get system status for microservice '%s'", microservice_id)
                system_status = None

            if system_status is not None and system_status.get("running", False) is True:
                return True

            time.sleep(sleep_time)

            timeout -= sleep_time

        return False

    def microservice_get_proxy_request(self, microservice_id, path, params=None, result_as_json=True):

        system = self.get_system(microservice_id)
        if system is None:
            raise AssertionError("Microservice system '%s' doesn't exist" % microservice_id)

        system_url = self.api_connection.get_system_url(microservice_id)
        resp = self.api_connection.session.get(system_url + "/proxy/" + path, params=params)
        resp.raise_for_status()

        if result_as_json:
            return resp.json()

        return resp.text

    def microservice_post_proxy_request(self, microservice_id, path, params=None, data=None, result_as_json=True):
        return self.microservice_post_put_proxy_request(microservice_id, "POST", path, params=params, data=data,
                                                        result_as_json=result_as_json)

    def microservice_put_proxy_request(self, microservice_id, path, params=None, data=None, result_as_json=True):
        return self.microservice_post_put_proxy_request(microservice_id, "PUT", path, params=params, data=data,
                                                        result_as_json=result_as_json)

    def microservice_post_put_proxy_request(self, microservice_id, method, path, params=None, data=None,
                                            result_as_json=True):

        system = self.get_system(microservice_id)
        if system is None:
            raise AssertionError("Microservice system '%s' doesn't exist" % microservice_id)

        system_url = self.api_connection.get_system_url(microservice_id)
        if method.lower() == "post":
            resp = self.api_connection.session.post(system_url + "/proxy/" + path, params=params, data=data)
        elif method.lower() == "put":
            resp = self.api_connection.session.put(system_url + "/proxy/" + path, params=params, data=data)
        else:
            raise AssertionError("Unknown method '%s'" % method)

        resp.raise_for_status()

        if result_as_json:
            return resp.json()

        return resp.text


class SesamCmdClient:
    """ Commands wrapped in a class to make it easier to write unit tests """

    def __init__(self, args, logger):
        self.args = args
        self.logger = logger
        self.sesam_node = None

    def read_config_file(self, filename):
        parser = configparser.ConfigParser(strict=False)

        with open(filename) as fp:
            parser.read_file(itertools.chain(['[sesam]'], fp), source=filename)
            config = {}
            for key, value in parser.items("sesam"):
                config[key.lower()] = value

            return config

    def _coalesce(self, items):
        for item in items:
            if item is not None:
                return item

    def zip_dir(self, zipfile, dir):
        for root, dirs, files in os.walk(dir):
            for file in files:
                if file.endswith(".conf.json"):
                    zipfile.write(os.path.join(root, file))

    def get_zip_config(self, remove_zip=True):
        """ Create a ZIP file from the local content on disk and return a bytes object
            If "remove_zip" is False, we dump it to disk as "sesam-config.zip" as well.
        """
        if os.path.isfile("sesam-config.zip"):
            os.remove("sesam-config.zip")

        zip_file = zipfile.ZipFile('sesam-config.zip', 'w', zipfile.ZIP_DEFLATED)

        self.zip_dir(zip_file, "pipes")
        self.zip_dir(zip_file, "systems")

        if os.path.isfile("node-metadata.conf.json"):
            zip_file.write("node-metadata.conf.json")

        zip_file.close()

        with open("sesam-config.zip", "rb") as fp:
            zip_data = fp.read()


        if remove_zip:
            os.remove("sesam-config.zip")

        return zip_data

    def get_zipfile_data_by_filename(self, zip_data, filename):
        zin = zipfile.ZipFile(io.BytesIO(zip_data))

        for item in zin.infolist():
            if item.filename == filename:
                return zin.read(item.filename)

        zin.close()
        return None

    def replace_file_in_zipfile(self, zip_data, filename, replacement):
        zin = zipfile.ZipFile(io.BytesIO(zip_data))
        buffer = io.BytesIO()
        zout = zipfile.ZipFile(buffer, mode="w")

        for item in zin.infolist():
            if item.filename == filename:
                zout.writestr(item, replacement)
            else:
                zout.writestr(item, zin.read(item.filename))

        zout.close()
        zin.close()

        buffer.seek(0)
        return buffer.read()

    def remove_task_manager_settings(self, zip_data):
        node_metadata = {}
        if os.path.isfile("node-metadata.conf.json"):
            with open("node-metadata.conf.json", "r") as infile:
                node_metadata = json.load(infile)

        if node_metadata.get("task_manager", {}).get("disable_user_pipes", False) is True:
            # No need to do anything, the setting is originally in the file!
            return zip_data

        remote_data = self.get_zipfile_data_by_filename(zip_data, "node-metadata.conf.json")
        if remote_data:
            remote_metadata = json.loads(str(remote_data, encoding="utf-8"))

            if "task_manager" in remote_metadata and "disable_user_pipes" in remote_metadata["task_manager"] and \
                            remote_metadata["task_manager"]["disable_user_pipes"] is True:
                remote_metadata["task_manager"].pop("disable_user_pipes")
                # Remove the entire task_manager section if its empty
                if len(remote_metadata["task_manager"]) == 0:
                    remote_metadata.pop("task_manager")

                # Replace the file and return the new zipfile
                return self.replace_file_in_zipfile(zip_data, "node-metadata.conf.json",
                                                    json.dumps(remote_metadata, indent=2,
                                                               ensure_ascii=False).encode("utf-8"))

        return zip_data

    def get_node_and_jwt_token(self):
        try:
            curr_dir = os.getcwd()
            if curr_dir is None:
                self.logger.error("Failed to open current directory. Check your permissions.")
                raise AssertionError("Failed to open current directory. Check your permissions.")

            # Find config on disk, if any
            try:
                file_config = {}
                if os.path.isfile(".syncconfig"):
                    # Found a local .syncconfig file, read it
                    file_config = self.read_config_file(".syncconfig")
                else:
                    # Look in the parent folder
                    if os.path.isfile("../.syncconfig"):
                        file_config = self.read_config_file("../.syncconfig")
                        if file_config:
                            curr_dir = os.path.abspath("../")
                            self.logger.info("Found .syncconfig in parent path. Using %s as base directory" % curr_dir)
                            os.chdir(curr_dir)

                self.logger.info("Using %s as base directory", curr_dir)

                self.node_url = self._coalesce([args.node, os.environ.get("NODE"), file_config.get("node")])
                self.jwt_token = self._coalesce([args.jwt, os.environ.get("JWT"), file_config.get("jwt")])

                if self.jwt_token and self.jwt_token.startswith('"') and self.jwt_token[-1] == '"':
                    self.jwt_token = self.jwt_token[1:-1]

                if self.jwt_token.startswith("bearer "):
                    self.jwt_token = self.jwt_token.replace("bearer ", "")

                if self.jwt_token.startswith("Bearer "):
                    self.jwt_token = self.jwt_token.replace("Bearer ", "")

                self.node_url = self.node_url.replace('"', "")

                if not self.node_url.startswith("http"):
                    self.node_url = "https://%s" % self.node_url

                if not self.node_url[-4:] == "/api":
                    self.node_url = "%s/api" % self.node_url

                return self.node_url, self.jwt_token
            except BaseException as e:
                self.logger.error("Failed to read '.syncconfig' from either the current directory or the "
                                  "parent directory. Check that you are in the correct directory, that you have the"
                                  "required permissions to read the files and that the files have the correct format.")
                raise e

        except BaseException as e:
            logger.error("Failed to find node url and/or jwt token")
            raise e

    def upload(self):
        # Find env vars to upload
        profile_file = "%s-env.json" % self.args.profile
        try:
            with open(profile_file, "r", encoding="utf-8-sig") as fp:
                json_data = json.load(fp)
        except BaseException as e:
            self.logger.error("Failed to parse profile: '%s'" % profile_file)
            raise e

        try:
            self.sesam_node.put_env(json_data)
        except BaseException as e:
            self.logger.error("Failed to replace environment variables in Sesam")
            raise e

        # Zip the relevant directories and upload to Sesam
        try:
            zip_config = self.get_zip_config(remove_zip=args.dump is False)

            # Modify the node-metadata.conf.json to stop the pipe scheduler
            if self.args.disable_user_pipes and os.path.isfile("node-metadata.conf.json"):
                with open("node-metadata.conf.json", "r") as infile:
                    node_metadata = json.load(infile)
                    if "task_manager" not in node_metadata:
                        node_metadata["task_manager"] = {}

                    node_metadata["task_manager"]["disable_user_pipes"] = True

                    zip_config = self.replace_file_in_zipfile(zip_config, "node-metadata.conf.json",
                                                              json.dumps(node_metadata).encode("utf-8"))
        except BaseException as e:
            logger.error("Failed to create zip archive of config")
            raise e

        try:
            self.sesam_node.put_config(zip_config, force=True)
        except BaseException as e:
            self.logger.error("Failed to upload config to sesam")
            raise e

        self.logger.info("Config uploaded successfully")

    def dump(self):
        try:
            zip_config = self.get_zip_config(remove_zip=False)
        except BaseException as e:
            logger.error("Failed to create zip archive of config")
            raise e

    def download(self):
        if not self.args.custom_scheduler:
            # Remove the scheduler, if it exists - we never want it in the downloaded config
            system = self.sesam_node.get_system(args.scheduler_id)
            if system is not None:
                try:
                    self.sesam_node.remove_system(args.scheduler_id)
                except BaseException as e:
                    self.logger.error("Failed to remove the scheduler system '%s'" % self.args.scheduler_id)
                    raise e

        if self.args.dump:
            if os.path.isfile("sesam-config.zip"):
                    os.remove("sesam-config.zip")

            zip_data = self.sesam_node.get_config(binary=True)
            zip_data = self.remove_task_manager_settings(zip_data)

            with open("sesam-config.zip", "wb") as fp:
                fp.write(zip_data)

            self.logger.info("Dumped downloaded config to 'sesam-config.zip'")
        else:
            zip_data = self.sesam_node.get_config(binary=True)
            zip_data = self.remove_task_manager_settings(zip_data)

        try:
            zip_config = zipfile.ZipFile(io.BytesIO(zip_data))
            zip_config.extractall()
        except BaseException as e:
            self.logger.error("Failed to unzip config file from Sesam to current directory")
            raise e

        zip_config.close()

        self.logger.info("Replaced local config successfully")

    def status(self):
        logger.error("Comparing local and node config...")

        if not self.args.custom_scheduler and self.args.dont_remove_scheduler is False:
            # Remove the scheduler, if it exists
            system = self.sesam_node.get_system(args.scheduler_id)
            if system is not None:
                try:
                    self.sesam_node.remove_system(args.scheduler_id)
                except BaseException as e:
                    self.logger.error("Failed to remove the scheduler system '%s'" % self.args.scheduler_id)
                    raise e

        local_config = zipfile.ZipFile(io.BytesIO(self.get_zip_config()))
        if self.args.dump:
            zip_data = self.sesam_node.get_config(binary=True)
            zip_data = self.remove_task_manager_settings(zip_data)

            with open("sesam-config.zip", "wb") as fp:
                fp.write(zip_data)

            self.logger.info("Dumped downloaded config to 'sesam-config.zip'")
            remote_config = zipfile.ZipFile(io.BytesIO(zip_data))
        else:
            remote_config = self.sesam_node.get_config(binary=True)
            remote_config = self.remove_task_manager_settings(remote_config)

        remote_files = sorted(remote_config.namelist())
        local_files = sorted(local_config.namelist())

        diff_found = False
        for remote_file in remote_files:
            if remote_file not in local_files:
                self.logger.info("Sesam file '%s' was not found locally" % remote_file)
                diff_found = True

        for local_file in local_files:
            if local_file not in remote_files:
                self.logger.info("Local file '%s' was not found in Sesam" % local_file)
                diff_found = True
            else:
                local_file_data = str(local_config.read(local_file), encoding="utf-8")
                remote_file_data = str(remote_config.read(local_file), encoding="utf-8")

                if local_file_data != remote_file_data:
                    self.logger.info("File '%s' differs from Sesam!" % local_file)

                    diff = self.get_diff_string(local_file_data, remote_file_data, local_file, local_file)
                    logger.info("Diff:\n%s" % diff)

                    diff_found = True

        if diff_found:
            logger.info("Sesam config is NOT in sync with local config!")
        else:
            logger.info("Sesam config is up-to-date with local config!")

    def filter_entity(self, entity, test_spec):
        """ Remove most underscore keys and filter potential blacklisted keys """
        def filter_item(parent_path, item):
            result = copy.deepcopy(item)
            if isinstance(item, dict):
                for key, value in item.items():
                    path = parent_path + [key]
                    if key.startswith("_"):
                        if key == "_id" or (key == "_deleted" and value is True):
                            continue
                        result.pop(key)
                    elif test_spec.is_path_blacklisted(path):
                        result.pop(key)
                    else:
                        result[key] = filter_item(path, value)
                return result
            elif isinstance(item, list):
                result = []
                for list_item in item:
                    result.append(filter_item(parent_path, list_item))
                return result

            return item

        return filter_item([], entity)

    def load_test_specs(self, existing_output_pipes, update=False):
        test_specs = {}

        # Load test specifications
        for filename in glob.glob("expected/*.test.json"):
            self.logger.debug("Processing spec file '%s'" % filename)

            test_spec = TestSpec(filename)

            pipe_id = test_spec.pipe
            self.logger.log(LOGLEVEL_TRACE, "Pipe id for spec '%s' is '%s" % (filename, pipe_id))

            # If spec says 'ignore' then the corresponding output file should not exist
            if test_spec.ignore is True:
                output_filename = test_spec.file

                if os.path.isfile("expected/%s" % output_filename):
                    if update:
                        self.logger.debug("Removing existing output file '%s'" % output_filename)
                        os.remove(output_filename)
                    else:
                        self.logger.warning(
                            "pipe '%s' is ignored, but output file '%s' still exists" % (pipe_id, filename))
            elif pipe_id not in existing_output_pipes:
                logger.error("Test spec references non-exisiting output "
                             "pipe '%s' - remove '%s'" % (pipe_id, filename))
                continue

            if pipe_id not in test_specs:
                test_specs[pipe_id] = []

            test_specs[pipe_id].append(test_spec)

        if update:
            for pipe in existing_output_pipes.values():
                self.logger.debug("Updating pipe '%s" % pipe.id)

                if pipe.id not in test_specs:
                    self.logger.warning("Found no spec for pipe %s - creating empty spec file" % pipe.id)

                    filename = "expected/%s.test.json" % pipe.id
                    with open(filename, "w") as fp:
                        fp.write("{\n}")
                    test_specs[pipe.id] = [TestSpec(filename)]

        return test_specs

    def get_diff_string(self, a, b, a_filename, b_filename):
        a_lines = io.StringIO(a).readlines()
        b_lines = io.StringIO(b).readlines()

        return "".join(unified_diff(a_lines, b_lines, fromfile=a_filename, tofile=b_filename))

    def bytes_to_xml_string(self, xml_data):

        xml_declaration, standalone = self.find_xml_header_settings(xml_data)
        xml_doc_root = etree.fromstring(xml_data)

        try:
            result = str(etree.tostring(xml_doc_root, encoding="utf-8",
                                        xml_declaration=xml_declaration,
                                        standalone=standalone,
                                        pretty_print=True), encoding="utf-8")
        except UnicodeEncodeError as e:
            result = str(etree.tostring(xml_doc_root, encoding="latin-1",
                                        xml_declaration=xml_declaration,
                                        standalone=standalone,
                                        pretty_print=True), encoding="latin-1")

        return result

    def _fix_decimal_to_ints(self, value):
        if isinstance(value, dict):
            for key, dict_value in value.items():
                value[key] = self._fix_decimal_to_ints(dict_value)
        elif isinstance(value, list):
            for ix, list_item in enumerate(value):
                value[ix] = self._fix_decimal_to_ints(list_item)
        else:
            if isinstance(value, (Decimal, float)):
                v = str(value)

                if v and v.endswith(".0"):
                    return self._fix_decimal_to_ints(int(value))
            elif not args.no_large_int_bugs and isinstance(value, int):
                v = str(value)
                if v and len(v) > len("9007199254740991"):
                    # Simulate go client bug :P
                    return int(Decimal(str(float(value))))

        return value

    def verify(self):
        self.logger.info("Verifying that expected output matches current output...")
        output_pipes = {}

        for p in self.sesam_node.get_output_pipes() + self.sesam_node.get_endpoint_pipes():
            output_pipes[p.id] = p

        test_specs = self.load_test_specs(output_pipes)

        if not test_specs:
            raise AssertionError("Found no tests (*.test.json) to run")

        failed_tests = []
        for pipe in output_pipes.values():
            self.logger.debug("Verifying pipe '%s'.." % pipe.id)

            if pipe.id in test_specs:
                # Verify all tests specs for this pipe
                for test_spec in test_specs[pipe.id]:
                    if test_spec.ignore is True:
                        self.logger.debug("Skipping test spec '%s' because it was marked as 'ignore'" % test_spec.name)
                        continue

                    if test_spec.endpoint == "json" or test_spec.endpoint == "excel":
                        # Get current entities from pipe in json form
                        expected_output = sorted(test_spec.expected_entities,
                                                 key=lambda e: (e['_id'],
                                                                json.dumps(e, ensure_ascii=False,
                                                                           sort_keys=True)))

                        current_output = sorted([self.filter_entity(e, test_spec)
                                                 for e in self.sesam_node.get_pipe_entities(pipe,
                                                                                            stage=test_spec.stage)],
                                                key=lambda e: e['_id'])

                        fixed_current_output = self._fix_decimal_to_ints(copy.deepcopy(current_output))

                        fixed_current_output = sorted(fixed_current_output,
                                                      key=lambda e: (e['_id'],
                                                                     json.dumps(e, ensure_ascii=False,
                                                                                sort_keys=True)))

                        if len(fixed_current_output) != len(expected_output):
                            msg = "Pipe verify failed! Length mismatch for test spec '%s': " \
                                  "expected %d got %d" % (test_spec.spec_file,
                                                          len(expected_output), len(fixed_current_output))
                            self.logger.error(msg)

                            self.logger.info("Expected output:\n%s", pprint.pformat(expected_output))

                            if self.args.extra_extra_verbose:
                                self.logger.info("Got raw output:\n%s", pprint.pformat(current_output))

                            self.logger.info("Got output:\n%s", pprint.pformat(fixed_current_output))

                            diff = self.get_diff_string(json.dumps(expected_output, indent=2,
                                                                   ensure_ascii=False, sort_keys=True),
                                                        json.dumps(fixed_current_output, indent=2, ensure_ascii=False,
                                                                   sort_keys=True),
                                                        test_spec.file, "current-output.json")
                            self.logger.info("Diff:\n%s" % diff)
                            failed_tests.append(test_spec)
                        else:
                            expected_json = json.dumps(expected_output,  ensure_ascii=False, indent=2, sort_keys=True)
                            current_json = json.dumps(fixed_current_output,  ensure_ascii=False, indent=2,
                                                      sort_keys=True)

                            if expected_json != current_json:
                                self.logger.error("Pipe verify failed! "
                                                  "Content mismatch for test spec '%s'" % test_spec.file)

                                self.logger.info("Expected output:\n%s" % pprint.pformat(expected_output))

                                if self.args.extra_extra_verbose:
                                    self.logger.info("Expected output JSON:\n%s" % expected_json)
                                    self.logger.info("Got raw output:\n%s" % pprint.pformat(current_output))
                                    self.logger.info("Got output JSON:\n%s" % current_json)

                                self.logger.info("Got output:\n%s" % pprint.pformat(fixed_current_output))

                                diff = self.get_diff_string(expected_json, current_json,
                                                            test_spec.file, "current-output.json")

                                self.logger.info("Diff:\n%s" % diff)
                                failed_tests.append(test_spec)

                    elif test_spec.endpoint == "xml":
                        # Special case: download and format xml document as a string
                        self.logger.debug("Comparing XML output..")
                        expected_output = test_spec.expected_data
                        current_output = self.sesam_node.get_published_data(pipe, "xml",
                                                                            params=test_spec.parameters,
                                                                            binary=True)

                        try:
                            # Compare prettified versions of expected and current output so we have the
                            # same serialisation to look at (XML documents may be semanticaly identical even if
                            # their serialisations differ).
                            expected_output = self.bytes_to_xml_string(expected_output)
                            current_output = self.bytes_to_xml_string(current_output)

                            if expected_output != current_output:
                                failed_tests.append(test_spec)

                                self.logger.info("Pipe verify failed! Content mismatch:\n%s" %
                                                  self.get_diff_string(expected_output, current_output, test_spec.file,
                                                                       "current_data.xml"))

                        except BaseException as e:
                            # Unable to parse the expected input and/or the current output, we'll have to just
                            # compare them byte-by-byte

                            self.logger.debug("Failed to parse expected output and/or current output as XML")
                            self.logger.debug("Falling back to byte-level comparison. Note that this might generate "
                                              "false differences for XML data.")

                            if expected_output != current_output:
                                failed_tests.append(test_spec)
                                self.logger.info("Pipe verify failed! Content mismatch!")
                    else:
                        # Download contents as-is as a byte buffer
                        expected_output = test_spec.expected_data
                        current_output = self.sesam_node.get_published_data(pipe, test_spec.endpoint,
                                                                            params=test_spec.parameters, binary=True)

                        if expected_output != current_output:
                            failed_tests.append(test_spec)

                            # Try to show diff - first try utf-8 encoding
                            try:
                                expected_output = str(expected_output, encoding="utf-8")
                                current_output = str(current_output, encoding="utf-8")
                            except UnicodeDecodeError as e:
                                try:
                                    expected_output = str(expected_output, encoding="latin-1")
                                    current_output = str(current_output, encoding="latin-1")
                                except UnicodeDecodeError as e2:
                                    self.logger.error("Pipe verify failed! Content mismatch!")
                                    self.logger.warning("Unable to read expected and/or output data as "
                                                        "unicode text so I can't show diff")
                                    continue

                            self.logger.error("Pipe verify failed! Content mismatch:\n%s" %
                                              self.get_diff_string(expected_output, current_output, test_spec.file,
                                                                   "current_data.txt"))

        if len(failed_tests) > 0:
            self.logger.error("Failed %s of %s tests!" % (len(failed_tests), len(list(test_specs.keys()))))
            self.logger.error("Failed pipe id (spec file):")
            for failed_test_spec in failed_tests:
                self.logger.error("%s (%s)" % (failed_test_spec.pipe, failed_test_spec.spec_file))

            raise RuntimeError("Verify failed")
        else:
            self.logger.info("All tests passed! Ran %s tests." % len(list(test_specs.keys())))

    def find_xml_header_settings(self, xml_data):
        xml_declaration = False
        standalone = None

        if xml_data.startswith(b"<?xml "):
            xml_declaration = True

            end_decl = xml_data.find(b"?>")
            if end_decl > -1:
                xmldecl = xml_data[0:end_decl]
                parts = xmldecl.split(b"standalone=")

                if len(parts) > 1:
                    arg = parts[1]
                    if arg.startswith(b'"'):
                        endix = arg[1:].find(b'"')
                        standalone = arg[1:endix]
                    elif arg.startswith(b"'"):
                        endix = arg[1:].find(b"'")
                        standalone = arg[1:endix]

        if standalone is not None:
            standalone = str(standalone, encoding="utf-8")

        return xml_declaration, standalone

    def update(self):
        self.logger.info("Updating expected output from current output...")
        output_pipes = {}

        for p in self.sesam_node.get_output_pipes() + self.sesam_node.get_endpoint_pipes():
            output_pipes[p.id] = p

        test_specs = self.load_test_specs(output_pipes, update=True)

        if not test_specs:
            raise AssertionError("Found no tests (*.test.json) to update")

        i = 0
        for pipe in output_pipes.values():
            if pipe.id in test_specs:
                self.logger.debug("Updating pipe '%s'.." % pipe.id)

                # Process all tests specs for this pipe
                for test_spec in test_specs[pipe.id]:
                    if test_spec.ignore is True:
                        self.logger.debug("Skipping test spec '%s' because it was marked as 'ignore'" % test_spec.name)
                        continue

                    self.logger.debug("Updating spec '%s' for pipe '%s'.." % (test_spec.name, pipe.id))
                    if test_spec.endpoint == "json" or test_spec.endpoint == "excel":
                        # Get current entities from pipe in json form
                        current_output = self._fix_decimal_to_ints([self.filter_entity(e, test_spec)
                                                                    for e in self.sesam_node.get_pipe_entities(
                                pipe, stage=test_spec.stage)])

                        current_output = sorted(current_output,
                                                key=lambda e: (e['_id'],
                                                               json.dumps(e,
                                                                          indent="  ",
                                                                          ensure_ascii=self.args.unicode_encoding,
                                                                          sort_keys=True)))

                        current_output = (json.dumps(current_output, indent="  ",
                                                     sort_keys=True,
                                                     ensure_ascii=self.args.unicode_encoding) +
                                          "\n").encode("utf-8")

                        if self.args.disable_json_html_escape is False:
                            current_output = current_output.replace(b"<", b"\\u003c")
                            current_output = current_output.replace(b">", b"\\u003e")
                            current_output = current_output.replace(b"&", b"\\u0026")

                    elif test_spec.endpoint == "xml":
                        # Special case: download and format xml document as a string
                        xml_data = self.sesam_node.get_published_data(pipe, "xml", params=test_spec.parameters,
                                                                      binary=True)
                        xml_doc_root = etree.fromstring(xml_data)

                        xml_declaration, standalone = self.find_xml_header_settings(xml_data)

                        current_output = etree.tostring(xml_doc_root, encoding="utf-8",
                                                        xml_declaration=xml_declaration,
                                                        standalone=standalone,
                                                        pretty_print=True)
                    else:
                        # Download contents as-is as a string
                        current_output = self.sesam_node.get_published_data(pipe, test_spec.endpoint,
                                                                            params=test_spec.parameters, binary=True)

                    test_spec.update_expected_data(current_output)
                    i += 1

        self.logger.info("%s tests updated!" % i)

    def test(self):
        try:
            self.logger.info("Running test: upload, run and verify..")
            self.upload()

            for i in range(self.args.runs):
                self.run()

            self.verify()
            self.logger.info("Test was successful!")
        except BaseException as e:
            self.logger.info("Test failed!")
            raise e

    def start_scheduler(self, timeout=300):
        try:
            if self.sesam_node.get_system(self.args.scheduler_id) is not None:
                self.logger.debug("Removing existing scheduler system...")
                self.sesam_node.remove_system(self.args.scheduler_id)

            if not self.args.custom_scheduler:
                # Override scheduler node url?
                if self.args.scheduler_node:
                    self.logger.debug("Overriding scheduler_node (%s)" % self.args.scheduler_node)
                    scheduler_node_url = self.args.scheduler_node
                else:
                    self.logger.debug("Reusing node_url since scheduler_node override not set")
                    scheduler_node_url = self.node_url

                scheduler_config = {
                    "_id": "%s" % self.args.scheduler_id,
                    "type": "system:microservice",
                    "docker": {
                        "environment": {
                            "JWT": "%s" % self.jwt_token,
                            "URL": "%s" % scheduler_node_url,
                            "DUMMY": "%s" % str(uuid.uuid4()),
                            "SCHEDULER_PORT": int(os.environ.get("SCHEDULER_PORT", 5000))
                        },
                        "memory": 512,
                        "image":  "sesamcommunity/scheduler:%s" % self.args.scheduler_image_tag,
                        "port": int(os.environ.get("SCHEDULER_PORT", 5000))
                    }
                }

                if int(os.environ.get("SCHEDULER_SKIP_PULL", 0)) == 1:
                    scheduler_config["docker"]["skip_pull"] = True

                if os.environ.get("SESAM_NOT_IN_DOCKER") is not None:
                    scheduler_config["docker"]["environment"]["SESAM_NOT_IN_DOCKER"] = True

                self.logger.debug("Adding scheduler microservice with configuration:\n%s" % scheduler_config)
                if self.sesam_node.add_system(scheduler_config, verify=True) is False:
                    self.logger.error("Scheduler config failed to load!")
                    raise RuntimeError("Failed to initialise scheduler")

            # Wait for Microservice system to start up
            if self.sesam_node.wait_for_microservice(self.args.scheduler_id, timeout=timeout) is False:
                raise RuntimeError("Timed out waiting for scheduler to load")

            # Check that it really has started up
            sleep_interval = args.scheduler_poll_frequency/1000
            while sleep_interval > 0:
                try:
                    status = self.get_scheduler_status()
                    if status == "init":
                        break
                except BaseException as e:
                    pass

                time.sleep(sleep_interval)
                timeout -= sleep_interval

            if sleep_interval <= 0:
                self.logger.error("Scheduler failed to initialise after %s seconds" % timeout)
                raise RuntimeError("Failed to initialise scheduler")

            # Start the microservice
            params = {
                "reset_pipes": "true", "delete_datasets": "true",
                "zero_runs": self.args.scheduler_zero_runs
            }

            if self.args.compact_execution_datasets:
                params["compact_execution_datasets"] = "true"

            if self.args.disable_user_pipes is True:
                # If we have stopped all the user pipes globally, the scheduler doesn't have to disable them
                params["disable_pipes"] = "false"

            self.logger.debug("Starting the scheduler...")
            self.sesam_node.microservice_post_proxy_request(self.args.scheduler_id, "start", params=params,
                                                            result_as_json=False)
            self.logger.debug("Scheduler started")
        except BaseException as e:
            self.logger.error("Failed to start the scheduler microservice")
            self.logger.debug("Scheduler log: %s", self.sesam_node.get_system_log(self.args.scheduler_id))

            if self.args.extra_verbose is True:
                self.logger.exception(e)

            if self.args.dont_remove_scheduler is False:
                try:
                    self.logger.debug("Removing scheduler microservice")
                    self.sesam_node.remove_system(self.args.scheduler_id)
                except BaseException as e2:
                    self.logger.error("Failed to remove scheduler microservice")
                    if self.args.extra_verbose is True:
                        self.logger.exception(e2)
            else:
                self.logger.debug("Leaving scheduler microservice in sesam")

    def get_scheduler_status(self):
        try:
            status_json = self.sesam_node.microservice_get_proxy_request(self.args.scheduler_id, "")
            if isinstance(status_json, dict):
                return status_json["state"]

            self.logger.debug("The scheduler status endpoint returned a non-json reply! %s" % str(status_json))
        except BaseException as e:
            logger.debug("Failed to get scheduler status")
            raise e

        return "unknown"

    def print_scheduler_log(self, since=None):
        try:
            if since is not None:
                log_output = self.sesam_node.get_system_log(self.args.scheduler_id, params={"since": since})
            else:
                log_output = self.sesam_node.get_system_log(self.args.scheduler_id)

            last_since = None
            for log_line in [e for e in log_output.split("\n") if e]:
                log_line = log_line.split(" ")
                last_since = log_line[0]
                logger.info(" ".join(log_line[1:]))

            return last_since
        except BaseException as e:
            if since is not None:
                self.logger.warning("Failed to get scheduler log for since value '%s'.." % since)
            else:
                self.logger.warning("Failed to get scheduler log..")

            return since

    def run_internal_scheduler(self):
        start_time = time.monotonic()

        disable_pipes = self.args.disable_user_pipes is False

        class SchedulerRunner(threading.Thread):
            def __init__(self, sesam_node):
                super().__init__()
                self.sesam_node = sesam_node
                self.status = None
                self.result = None

            def run(self):
                try:
                    self.result = self.sesam_node.run_internal_scheduler(disable_pipes=disable_pipes)
                    self.status = "finished"
                except BaseException as e:
                    self.status = "failed"
                    self.result = e

        scheduler_runner = SchedulerRunner(self.sesam_node)
        scheduler_runner.start()

        time.sleep(1)

        since = None

        def print_internal_scheduler_log(since_val):
            log_lines = self.sesam_node.get_internal_scheduler_log(since=since_val)
            for log_line in log_lines:
                s = "%s - %s - %s" % (log_line["timestamp"], log_line["loglevel"], log_line["logdata"])
                logger.info(s)

            if len(log_lines) > 0:
                return log_lines[-1]["timestamp"]

            return since_val

        while True:
            since = print_internal_scheduler_log(since)
            if scheduler_runner.status is not None:
                break

            time.sleep(1)

        if scheduler_runner.status == "failed":
            self.logger.info("Failed to run pipes to completion")
            print_internal_scheduler_log(since)
            raise scheduler_runner.result

        print_internal_scheduler_log(since)
        self.logger.info("Successfully ran all pipes to completion in %s seconds" % int(time.monotonic() - start_time))

        return 0

    def run(self):
        if self.args.use_internal_scheduler:
            return self.run_internal_scheduler()
        else:
            start_time = time.monotonic()
            try:
                self.logger.info("Executing scheduler...")
                self.start_scheduler()

                since = None
                while True:
                    if self.args.print_scheduler_log:
                        since = self.print_scheduler_log(since=since)

                    status = self.get_scheduler_status()

                    if status == "success":
                        self.logger.debug("Scheduler finished successfully")
                        break
                    elif status == "failed":
                        self.logger.error("Scheduler finished with failure")
                        return -1

                    time.sleep(args.scheduler_poll_frequency/1000)

            except BaseException as e:
                self.logger.error("Failed to run scheduler")
                raise e
            finally:
                end_time = time.monotonic()
                if self.args.dont_remove_scheduler is False:
                    self.sesam_node.remove_system(args.scheduler_id)

            self.logger.info("Successfully ran all pipes to completion in %s seconds" % int(end_time - start_time))
            return 0

    def wipe(self):
        self.logger.info("Wiping node...")

        try:
            self.sesam_node.put_config([], force=True)
            self.logger.info("Removed pipes and systems")
        except BaseException as e:
            logger.error("Failed to wipe config")
            raise e

        try:
            self.sesam_node.put_env({})
            self.logger.info("Removed environment variables")
        except BaseException as e:
            logger.error("Failed to wipe environment variables")
            raise e

        try:
            self.sesam_node.remove_all_datasets()
            self.logger.info("Removed datasets")
        except BaseException as e:
            self.logger.error("Failed to delete datasets")
            raise e

        self.logger.info("Successfully wiped node!")


if __name__ == '__main__':
    parser = SesamParser(prog="sesam", description="""
Commands:
  wipe      Deletes all the pipes, systems, user datasets and environment variables in the node
  upload    Replace node config with local config
  download  Replace local config with node config
  dump      Create a zip archive of the config and store it as 'sesam-config.zip'
  status    Compare node config with local config (requires external diff command)
  run       Run configuration until it stabilizes
  update    Store current output as expected output
  verify    Compare output against expected output
  test      Upload, run and verify output
""", formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument('-version', dest='version', required=False, action='store_true', help="print version number")

    parser.add_argument('-v', dest='verbose', required=False, action='store_true', help="be verbose")

    parser.add_argument('-vv', dest='extra_verbose', required=False, action='store_true', help="be extra verbose")

    parser.add_argument('-vvv', dest='extra_extra_verbose', required=False, action='store_true',
                        help="be extra extra verbose")

    parser.add_argument('-skip-tls-verification', dest='skip_tls_verification', required=False, action='store_true',
                        help="skip verifying the TLS certificate")

    parser.add_argument('-dont-remove-scheduler', dest='dont_remove_scheduler', required=False, action='store_true',
                        help="don't remove scheduler after failure")

    parser.add_argument('-dump', dest='dump', required=False, help="dump zip content to disk", action='store_true')

    parser.add_argument('-print-scheduler-log', dest='print_scheduler_log', required=False,
                        help="print scheduler log during run", action='store_true')

    parser.add_argument('-use-internal-scheduler', dest='use_internal_scheduler', required=False,
                        help="use the built-in scheduler in sesam instead of a microservice", action='store_true')

    parser.add_argument('-custom-scheduler', dest='custom_scheduler', required=False,
                        help="by default a scheduler system will be added, enable this flag if you have configured a "
                             "custom scheduler as part of the config", action='store_true')

    parser.add_argument('-scheduler-image-tag', dest='scheduler_image_tag', required=False,
                        help="the scheduler image tag to use", type=str,
                        metavar="<string>", default="latest")

    parser.add_argument('-node', dest='node', metavar="<string>", required=False, help="service url")
    parser.add_argument('-scheduler-node', dest='scheduler_node', metavar="<string>", required=False, help="service url for scheduler")
    parser.add_argument('-jwt', dest='jwt', metavar="<string>", required=False, help="authorization token")

    parser.add_argument('-single', dest='single', required=False, metavar="<string>", help="update or verify just a single pipe")

    parser.add_argument('-no-large-int-bugs', dest='no_large_int_bugs', required=False, action='store_true',
                        help="don't reproduce old large int bugs")

    parser.add_argument('-disable-user-pipes', dest='disable_user_pipes', required=False, action='store_true',
                        help="turn off user pipe scheduling in the target node")

    parser.add_argument('-compact-execution-datasets', dest='compact_execution_datasets', required=False, action='store_true',
                        help="compact all execution datasets when running scheduler")

    parser.add_argument('-unicode-encoding', dest='unicode_encoding', required=False, action='store_true',
                        help="store the 'expected output' json files using unicode encoding ('\\uXXXX') - "
                             "the default is UTF-8")

    parser.add_argument('-disable-json-html-escape', dest='disable_json_html_escape',
                        required=False, action='store_true',
                        help="turn off escaping of '<', '>' and '&' characters in 'expected output' json files")

    parser.add_argument('-profile', dest='profile', metavar="<string>", default="test", required=False, help="env profile to use <profile>-env.json")

    parser.add_argument('-scheduler-id', dest='scheduler_id', default="scheduler", metavar="<string>", required=False, help="system id for the scheduler system")

    parser.add_argument('-scheduler-zero-runs', dest='scheduler_zero_runs', default=2, metavar="<int>", type=int, required=False,
                        help="the number of runs that has to yield zero changes for the scheduler to finish")

    parser.add_argument('-runs', dest='runs', type=int, metavar="<int>", required=False, default=1,
                        help="number of test cycles to check for stability")

    parser.add_argument('-logformat', dest='logformat', type=str, metavar="<string>", required=False, default="short",
                        help="output format (normal or log)")

    parser.add_argument('-scheduler-poll-frequency', metavar="<int>", dest='scheduler_poll_frequency', type=int, required=False,
                        default=5000, help="milliseconds between each poll while waiting for the scheduler")

    parser.add_argument('command', metavar="command", nargs='?', help="a valid command from the list above")

    try:
        args = parser.parse_args()
    except SystemExit as e:
        sys.exit(e.code)
    except BaseException as e:
        sys.exit(1)

    if args.version:
        print("sesam version %s" % sesam_version)
        sys.exit(0)

    if args.logformat == "log":
        format_string = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    else:
        format_string = '%(message)s'

    # Log to stdout
    logging.addLevelName(LOGLEVEL_TRACE, "TRACE")
    stdout_handler = logging.StreamHandler()
    stdout_handler.setFormatter(logging.Formatter(format_string))
    logger.addHandler(stdout_handler)

    if args.verbose:
        logger.setLevel(logging.DEBUG)
    elif args.extra_verbose:
        logger.setLevel(LOGLEVEL_TRACE)
    elif args.extra_extra_verbose:
        from http.client import HTTPConnection
        HTTPConnection.debuglevel = 1
        logging.basicConfig()
        logging.getLogger().setLevel(logging.DEBUG)
        requests_log = logging.getLogger("requests.packages.urllib3")
        requests_log.setLevel(logging.DEBUG)
        requests_log.propagate = True
        logger.setLevel(LOGLEVEL_TRACE)
    else:
        logger.setLevel(logging.INFO)

    logger.propagate = False

    command = args.command and args.command.lower() or ""

    if command not in ["upload", "download", "status", "update", "verify", "test", "run", "wipe", "dump"]:
        if command:
            logger.error("Unknown command: '%s'", command)
        else:
            logger.error("No command given")

        parser.print_usage()
        sys.exit(1)

    sesam_cmd_client = SesamCmdClient(args, logger)

    try:
        node_url, jwt_token = sesam_cmd_client.get_node_and_jwt_token()
    except BaseException as e:
        if args.verbose is True or args.extra_verbose is True or args.extra_extra_verbose is True:
            logger.exception(e)
        logger.error("jwt and node must be specified either as parameter, os env or in config file")
        sys.exit(1)

    try:
        sesam_cmd_client.sesam_node = SesamNode(node_url, jwt_token, logger,
                                                verify_ssl=args.skip_tls_verification is False)
    except BaseException as e:
        if args.verbose or args.extra_verbose:
            logger.exception(e)
        logger.error("failed to connect to the sesam node using the url and jwt token we were given:\n%s\n%s" %
                     (node_url, jwt_token))
        logger.error("please verify the url and token is correct, and that there isn't any network issues "
                     "(i.e. firewall, internet connection etc)")
        sys.exit(1)

    start_time = time.monotonic()
    try:
        if command == "upload":
            sesam_cmd_client.upload()
        elif command == "download":
            sesam_cmd_client.download()
        elif command == "status":
            sesam_cmd_client.status()
        elif command == "update":
            sesam_cmd_client.update()
        elif command == "verify":
            sesam_cmd_client.verify()
        elif command == "test":
            sesam_cmd_client.test()
        elif command == "run":
            if args.disable_user_pipes is True:
                logger.warning("Note that the -disable-user-pipes flag has no effect on the actual node configuration "
                               "outside the 'upload' or 'test' commands")
            sesam_cmd_client.run()
        elif command == "wipe":
            sesam_cmd_client.wipe()
        elif command == "dump":
            sesam_cmd_client.dump()
        else:
            logger.error("Unknown command: %s" % command)
            sys.exit(1)
    except BaseException as e:
        logger.error("Sesam client failed!")
        if args.extra_verbose is True or args.extra_extra_verbose is True:
            logger.exception("Underlying exception was: %s" % str(e))

        sys.exit(1)
    finally:
        run_time = time.monotonic() - start_time
        logger.info("Total run time was %d seconds" % run_time)
