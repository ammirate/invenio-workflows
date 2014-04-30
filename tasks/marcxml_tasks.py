# -*- coding: utf-8 -*-
## This file is part of Invenio.
## Copyright (C) 2013, 2014 CERN.
##
## Invenio is free software; you can redistribute it and/or
## modify it under the terms of the GNU General Public License as
## published by the Free Software Foundation; either version 2 of the
## License, or (at your option) any later version.
##
## Invenio is distributed in the hope that it will be useful, but
## WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
## General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with Invenio; if not, write to the Free Software Foundation, Inc.,
## 59 Temple Place, Suite 330, Boston, MA 02111-1307, USA.

import os
import random
import time
import glob
import re
import traceback

from sqlalchemy.orm.exc import MultipleResultsFound, NoResultFound

from invenio.base.globals import cfg
from invenio.base.wrappers import lazy_import

from invenio.utils.shell import (run_shell_command,
                                 Timeout)
from invenio.utils.plotextractor.converter import untar
from invenio.modules.workflows.utils import convert_marcxml_to_bibfield

bibtask = lazy_import("invenio.legacy.bibsched.bibtask")
records_api = lazy_import("invenio.modules.records.api")
workflows_error = lazy_import("invenio.modules.workflows.errors")
plotextractor_getter = lazy_import("invenio.utils.plotextractor.getter")

REGEXP_REFS = re.compile(
    "<record.*?>.*?<controlfield .*?>.*?</controlfield>(.*?)</record>",
    re.DOTALL)
REGEXP_AUTHLIST = re.compile(
    "<collaborationauthorlist>.*?</collaborationauthorlist>", re.DOTALL)


def add_metadata_to_extra_data(obj, eng):
    """
    Creates bibrecord from object data and
    populates extra_data with metadata
    :param obj: Bibworkflow Object to process
    :param eng: BibWorkflowEngine processing the object
    """
    obj.extra_data["_last_task_name"] = "add_metadata_to_extra_data"
    from invenio.legacy.bibrecord import create_record as old_create_record, record_get_field_value

    record = old_create_record(obj.data)
    obj.extra_data['redis_search']['category'] = \
        record_get_field_value(record[0], '037', code='c')
    obj.extra_data['redis_search']['title'] = \
        record_get_field_value(record[0], '245', code='a')
    obj.extra_data['redis_search']['source'] = \
        record_get_field_value(record[0], '035', code='9')


def approve_record(obj, eng):
    """
    Will add the approval action to the record.
    The workflow need to be halted to use the
    action in the holdingpen.
    :param obj: Bibworkflow Object to process
    :param eng: BibWorkflowEngine processing the object
    """
    try:
        eng.halt(action="approval",
                 msg='Record needs approval')
    except KeyError:
        # Log the error
        obj.extra_data["_error_msg"] = 'Could not assign action'


def filtering_oai_pmh_identifier(obj, eng):
    """
    Get the OAI-PMH identifier in the OAI request.

    :param obj: BibworkflowObject being processed
    :param eng: BibWorkflowEngine processing the object
    """
    if "oaiharvest" not in eng.extra_data:
        eng.extra_data["oaiharvest"] = {}
    if "identifiers" not in eng.extra_data["oaiharvest"]:
        eng.extra_data["oaiharvest"]["identifiers"] = []

    if not isinstance(obj.data, list):
        obj_data_list = [obj.data]
    else:
        obj_data_list = obj.data
    for record in obj_data_list:
        delimiter_start = "<identifier>"
        delimiter_end = "</identifier>"
        identifier = record[
            record.index(delimiter_start) +
            len(delimiter_start):record.index(delimiter_end)
        ]
        if identifier in eng.extra_data["oaiharvest"]["identifiers"]:
            return False
        else:
            eng.extra_data["oaiharvest"]["identifiers"].append(identifier)
            return True


def inspire_filter_custom(fields, custom_accepted=(), custom_refused=(),
                          custom_widgeted=(), action=None):
    """
    This function allow you to filter for any type of key in a dictionnary stored
    in object data.

    :param fields: list representing field to go into for filtering ['a','b'] means that we
    will first look into 'a' key in the dict then from 'a' key the 'b' key inside.
    :type fields: list

    :param custom_accepted: list of values that can be accepted
    :type custom_accepted: list
    :param custom_refused: list of value that must be refused
    :type custom_refused: list
    :param custom_widgeted: list of value that trigger a widget
    :type custom_widgeted: list
    :param widget: widget triggered if a value in custom_widgeted is found.
    :return: function to be intepreted by the workflow engine
    """

    def _inspire_filter_custom(obj, eng):

        custom_to_process_current = []
        custom_to_process_next = []
        action_to_take = [0, 0, 0]

        fields_to_process = fields
        if not isinstance(fields_to_process, list):
            fields_to_process = [fields_to_process]

        for field in fields_to_process:
            if len(custom_to_process_current) == 0:
                if len(fields_to_process) == 1:
                    custom_to_process_next.append(obj.data[field])
                custom_to_process_current.append(obj.data[field])
            else:
                while len(custom_to_process_current) > 0:
                    one_custom = custom_to_process_current.pop()
                    if isinstance(one_custom, list):
                        for i in one_custom:
                            custom_to_process_current.append(i)
                    else:
                        try:
                            custom_to_process_next.append(one_custom[field])
                        except KeyError:
                            eng.log.error(
                                "no " + str(field) + " in " + str(one_custom))
                custom_to_process_current = custom_to_process_next[:]
        if not custom_to_process_next:
            eng.log.error(
                "%s not found in the record. Human intervention needed",
                fields_to_process)
            eng.halt(str(
                fields_to_process) + " not found in the record. Human intervention needed",
                     action=action)
        for i in custom_widgeted:
            if i != '*':
                i = re.compile('^' + re.escape(i) + '.*')
                for y in custom_to_process_next:
                    if i.match(y):
                        action_to_take[0] += 1

        for i in custom_accepted:
            if i != '*':
                i = re.compile('^' + re.escape(i) + '.*')
                for y in custom_to_process_next:
                    if i.match(y):
                        action_to_take[1] += 1

        for i in custom_refused:
            if i != '*':
                i = re.compile('^' + re.escape(i) + '.*')
                for y in custom_to_process_next:
                    if i.match(y):
                        action_to_take[2] += 1

        sum_action = action_to_take[0] + action_to_take[1] + action_to_take[2]

        if sum_action == 0:
            #We allow the * option which means at final case
            if '*' in custom_widgeted:
                return None
            elif '*' in custom_refused:
                eng.stopProcessing()
            elif '*' in custom_accepted:
                return None
            else:
                # We don't know what we should do, in doubt query human...
                # they are nice!
                msg = ("Category out of task definition. "
                       "Human intervention needed")
                eng.halt(msg, action=action)
        else:
            if sum_action == action_to_take[0]:
                eng.halt("The %s of this record is %s, "
                         "this field is under filtering. "
                         "Should we accept this record ? "
                         % (fields[len(fields)-1], custom_to_process_next),
                         action=action)
            elif sum_action == action_to_take[1]:
                return None
            elif sum_action == action_to_take[2]:
                eng.stopProcessing()
            else:
                eng.halt("Category filtering needs human intervention, rules are incoherent !!!",
                         action=action)

    return _inspire_filter_custom


def inspire_filter_category(category_accepted_param=(),
                            category_refused_param=(),
                            category_widgeted_param=(), widget_param=None):
    """

    :param category_accepted_param:
    :param category_refused_param:
    :param category_widgeted_param:
    :param widget_param:
    :return:
    """

    def _inspire_filter_category(obj, eng):
        try:
            category_accepted = \
                obj.extra_data["_repository"]["arguments"]["filtering"][
                    'category_accepted']
        except KeyError:
            category_accepted = category_accepted_param
        try:
            category_refused = \
                obj.extra_data["_repository"]["arguments"]["filtering"][
                    'category_refused']
        except KeyError:
            category_refused = category_refused_param
        try:
            category_widgeted = \
                obj.extra_data["_repository"]["arguments"]["filtering"][
                    'category_widgeted']
        except KeyError:
            category_widgeted = category_widgeted_param
        try:
            action = obj.extra_data["_repository"]["arguments"]["filtering"]['action']
        except KeyError:
            action = widget_param

        category_to_process = []
        action_to_take = [0, 0, 0]
        try:
            category = obj.data["report_number"]
            if isinstance(category, list):
                for i in category:
                    category_to_process.append(i["arxiv_category"])
            else:
                category_to_process.append(category["arxiv_category"])
            obj.add_task_result("Category filter", category_to_process)
        except KeyError:
            msg = "Category not found in the record. Human intervention needed"
            eng.log.error(msg)
            eng.halt(msg, action=action)

        for i in category_widgeted:
            if i != '*':
                i = re.compile('^' + re.escape(i) + '.*')
                for y in category_to_process:
                    if i.match(y):
                        action_to_take[0] += 1

        for i in category_accepted:
            if i != '*':
                i = re.compile('^' + re.escape(i) + '.*')
                for y in category_to_process:
                    if i.match(y):
                        action_to_take[1] += 1

        for i in category_refused:
            if i != '*':
                i = re.compile('^' + re.escape(i) + '.*')
                for y in category_to_process:
                    if i.match(y):
                        action_to_take[2] += 1

        sum_action = action_to_take[0] + action_to_take[1] + action_to_take[2]

        if sum_action == 0:
            #We allow the * option which means at final case
            if '*' in category_accepted:
                return None
            elif '*' in category_refused:
                eng.stopProcessing()
            else:
                # We don't know what we should do, in doubt query human... they are nice!
                msg = ("Category out of task definition. "
                       "Human intervention needed")
                eng.halt(msg, action=action)
        else:
            if sum_action == action_to_take[0]:
                eng.halt("The category of this record is %s,"
                         "this category is under filtering."
                         "Should we accept this record ?" % category,
                         action=action)
            elif sum_action == action_to_take[1]:
                return None
            elif sum_action == action_to_take[2]:
                eng.stopProcessing()
            else:
                eng.halt(
                    "Category filtering needs human intervention, rules are incoherent !!!",
                     action=action)
    return _inspire_filter_category


def convert_record_to_bibfield(obj, eng):
    """
    Convert a record in data log.error into a 'dictionary'
    thanks to BibField

    :param obj: Bibworkflow Object to process
    :param eng: BibWorkflowEngine processing the object
    """
    obj.data = convert_marcxml_to_bibfield(obj.data)
    eng.log.info("Field conversion succeeded")


def init_harvesting(obj, eng):
    """
    This function gets all the option linked to the task and stores them into the
    object to be used later.

    :param obj: Bibworkflow Object to process
    :param eng: BibWorkflowEngine processing the object

    """
    try:
        obj.extra_data["options"] = eng.extra_data["options"]
    except KeyError:
        eng.log.error("No options for this task have been found. It is possible"
                      "that the following task could failed or work not as expected")
        obj.extra_data["options"] = {}
    eng.log.info("end of init_harvesting")


def get_repositories_list(repositories=()):
    """
    Here we are retrieving the oaiharvest configuration for the task.
    It will allows in the future to do all the correct operations.
    :param repositories:
    """
    from invenio.modules.oaiharvester.models import OaiHARVEST

    def _get_repositories_list(obj, eng):
        repositories_to_harvest = repositories
        reposlist_temp = []
        if obj.extra_data["options"]["repository"]:
            repositories_to_harvest = obj.extra_data["options"]["repository"]
        if repositories_to_harvest:
            for reposname in repositories_to_harvest:
                try:
                    reposlist_temp.append(
                        OaiHARVEST.get(OaiHARVEST.name == reposname).one())
                except (MultipleResultsFound, NoResultFound):
                    eng.log.critical("Repository %s doesn't exit into our database", reposname)
        else:
            reposlist_temp = OaiHARVEST.get(OaiHARVEST.name != "").all()
        true_repo_list = []
        for repo in reposlist_temp:
            true_repo_list.append(repo.to_dict())

        if true_repo_list:
            return true_repo_list
        else:
            eng.halt("No Repository named %s. Impossible to harvest non-existing things."
                     % repositories_to_harvest)

    return _get_repositories_list


def harvest_records(obj, eng):
    """
    Run the harvesting task.  The row argument is the oaiharvest task
    queue row, containing if, arguments, etc.
    Return 1 in case of success and 0 in case of failure.
    :param obj: BibworkflowObject being
    :param eng: BibWorkflowEngine processing the object
    """
    from invenio.legacy.oaiharvest.utils import (collect_identifiers,
                                                 harvest_step)

    harvested_identifier_list = []

    harvestpath = "%s_%d_%s_" % (
        "%s/oaiharvest_%s" % (cfg['CFG_TMPSHAREDDIR'], eng.uuid),
        1, time.strftime("%Y%m%d%H%M%S"))

    # ## go ahead: check if user requested from-until harvesting
    try:
        if "dates" not in obj.extra_data["options"]:
            obj.extra_data["options"]["dates"] = []
        if "identifiers" not in obj.extra_data["options"]:
            obj.extra_data["options"]["identifiers"] = []
    except TypeError:
        obj.extra_data["options"] = {"dates": [], "identifiers": []}

    bibtask.task_sleep_now_if_required()

    arguments = obj.extra_data["_repository"]["arguments"]
    if arguments:
        eng.log.info("running with post-processes: %r" % (arguments,))
    else:
        eng.log.error(
            "No arguments found... It can be causing major error after this point.")

    # Harvest phase

    try:
        harvested_files_list = harvest_step(obj,
                                            harvestpath)
    except Exception as e:
        eng.log.error("Error while harvesting %s. Skipping." % (obj.data,))

        raise workflows_error.WorkflowError(
            "Error while harvesting %r. Skipping : %s." % (obj.data, repr(e)),
            id_workflow=eng.uuid, id_object=obj.id)

    if len(harvested_files_list) == 0:
        eng.log.info("No records harvested for %s" % (obj.data["name"],))
        # Retrieve all OAI IDs and set active list

    harvested_identifier_list.append(collect_identifiers(harvested_files_list))

    if len(harvested_files_list) != len(harvested_identifier_list[0]):
        # Harvested files and its identifiers are 'out of sync', abort harvest

        raise workflows_error.WorkflowError(
            "Harvested files miss identifiers for %s" % (arguments,),
            id_workflow=eng.uuid,
            id_object=obj.id)
    obj.extra_data['harvested_files_list'] = harvested_files_list
    eng.log.info(
        "%d files harvested and processed \n End harvest records task" % (
            len(harvested_files_list),))


def get_records_from_file(path=None):
    """

    :param path:
    :return:
    """
    from invenio.legacy.oaiharvest.utils import record_extraction_from_file

    def _get_records_from_file(obj, eng):
        if "_LoopData" not in eng.extra_data:
            eng.extra_data["_LoopData"] = {}
        if "get_records_from_file" not in eng.extra_data["_LoopData"]:
            eng.extra_data["_LoopData"]["get_records_from_file"] = {}
            if path:
                eng.extra_data["_LoopData"]["get_records_from_file"].update(
                    {"data": record_extraction_from_file(path)})
            else:
                eng.extra_data["_LoopData"]["get_records_from_file"].update(
                    {"data": record_extraction_from_file(obj.data)})
                eng.extra_data["_LoopData"]["get_records_from_file"][
                    "path"] = obj.data

        elif os.path.isfile(obj.data) and obj.data != \
                eng.extra_data["_LoopData"]["get_records_from_file"]["path"]:
            eng.extra_data["_LoopData"]["get_records_from_file"].update(
                {"data": record_extraction_from_file(obj.data)})
        return eng.extra_data["_LoopData"]["get_records_from_file"]["data"]

    return _get_records_from_file


def get_eng_uuid_harvested(obj, eng):
    """
    Simple function which allows to retrieve the uuid of the eng in the workflow
    for printing by example

    :param obj: Bibworkflow Object to process
    :param eng: BibWorkflowEngine processing the object
    """
    eng.log.info("last task name: get_eng_uuid_harvested")
    return "*" + str(eng.uuid) + "*.harvested"


def get_files_list(path, parameter):
    """

    :param path:
    :param parameter:
    :return:
    """

    def _get_files_list(obj, eng):
        if callable(parameter):
            unknown = parameter
            while callable(unknown):
                unknown = unknown(obj, eng)

        else:
            unknown = parameter
        result = glob.glob1(path, unknown)
        for i in range(0, len(result)):
            result[i] = path + os.sep + result[i]
        return result

    return _get_files_list


def set_obj_extra_data_key(key, value):
    """

    :param value:
    :param key:
    """

    def _set_obj_extra_data_key(obj, eng):
        import six

        my_value = value
        my_key = key
        if six.callable(my_value):
            while six.callable(my_value):
                my_value = my_value(obj, eng)

        if six.callable(my_key):
            while six.callable(my_key):
                my_key = my_key(obj, eng)

        obj.extra_data[str(my_key)] = my_value

    return _set_obj_extra_data_key


def get_obj_extra_data_key(name):
    """

    :param name:
    :return:
    """

    def _get_obj_extra_data_key(obj, eng):
        return obj.extra_data[name]

    return _get_obj_extra_data_key


def get_eng_extra_data_key(name):
    """

    :param name:
    :return:
    """

    def _get_eng_extra_data_key(obj, eng):
        return eng.extra_data[name]

    return _get_eng_extra_data_key


def get_data(obj, eng):
    """

    :param obj:
    :param eng:
    :return:
    """
    return obj.data


def convert_record(stylesheet="oaidc2marcxml.xsl"):
    """

    :param stylesheet:
    :return: :raise workflows_error.WorkflowError:
    """

    def _convert_record(obj, eng):
        """
        Will convert the object data, if XML, using the given stylesheet
        """
        from invenio.legacy.bibconvert.xslt_engine import convert

        eng.log.info("Starting conversion using %s stylesheet" %
                     (stylesheet,))

        try:
            obj.data = convert(obj.data, stylesheet)
        except Exception as e:
            msg = "Could not convert record: %s\n%s" % \
                  (str(e), traceback.format_exc())
            obj.extra_data["_error_msg"] = msg
            raise workflows_error.WorkflowError("Error: %s" % (msg,),
                                                id_workflow=eng.uuid,
                                                id_object=obj.id)

    return _convert_record


def convert_record_with_repository(stylesheet="oaidc2marcxml.xsl"):
    """
    This function converts a record to a marcxml representation by using a
    style sheet which should be in parameter or which sould have been stored
    into extra data of the object.

    The priority is given to the stylesheet into the extra data of the object.
    The parameter should be use in case the stylesheet is missing from extra data
    or when you want to do a simple workflow which doesn't need to be dynamic.

    :param stylesheet: it is the name of the stylesheet that you want to use
    to convert a oai record to a marcxml one
    :type stylesheet: str
    """

    def _convert_record(obj, eng):
        """
        Will convert the object data, if XML, using the stylesheet
        in the OAIrepository stored in the object extra_data.
        """
        eng.log.info("my type: %s" % (obj.data_type,))
        try:
            if not obj.extra_data["_repository"]["arguments"]['c_stylesheet']:
                stylesheet_to_use = stylesheet
            else:
                stylesheet_to_use = obj.extra_data["_repository"]["arguments"][
                    'c_stylesheet']
        except KeyError:
            eng.log.error("WARNING: HASARDOUS BEHAVIOUR EXPECTED, "
                          "You didn't specified style_sheet in argument for conversion,"
                          "try to recover by using the default one!")
            stylesheet_to_use = stylesheet
        convert_record(stylesheet_to_use)(obj, eng)

    return _convert_record


def update_last_update(repository_list):
    """

    :param repository_list:
    :return:
    """
    from invenio.legacy.oaiharvest.dblayer import update_lastrun

    def _update_last_update(obj, eng):
        if "_should_last_run_be_update" in obj.extra_data:
            if obj.extra_data["_should_last_run_be_update"]:
                repository_list_to_process = repository_list
                if not isinstance(repository_list_to_process, list):
                    if callable(repository_list_to_process):
                        while callable(repository_list_to_process):
                            repository_list_to_process = repository_list_to_process(
                                obj, eng)
                    else:
                        repository_list_to_process = [
                            repository_list_to_process]
                for repository in repository_list_to_process:
                    update_lastrun(repository["id"])

    return _update_last_update


def fulltext_download(obj, eng):
    """
    Performs the fulltext download step.
    Only for arXiv

    :param obj: Bibworkflow Object to process
    :param eng: BibWorkflowEngine processing the object
    """
    if "result" not in obj.extra_data:
        obj.extra_data["_result"] = {}
    bibtask.task_sleep_now_if_required()
    if "pdf" not in obj.extra_data["_result"]:
        extract_path = plotextractor_getter.make_single_directory(
            cfg['CFG_TMPSHAREDDIR'], str(eng.uuid))
        tarball, pdf = plotextractor_getter.harvest_single(
            obj.data["system_number_external"]["value"],
            extract_path, ["pdf"])
        arguments = obj.extra_data["_repository"]["arguments"]
        try:
            if not arguments['t_doctype'] == '':
                doctype = arguments['t_doctype']
            else:
                doctype = 'arXiv'
        except KeyError:
            eng.log.error("WARNING: HASARDOUS BEHAVIOUR EXPECTED, "
                          "You didn't specified t_doctype in argument"
                          " for fulltext_download,"
                          "try to recover by using the default one!")
            doctype = 'arXiv'
        if pdf:
            obj.extra_data["_result"]["pdf"] = pdf
            fulltext_xml = ("  <datafield tag=\"FFT\" ind1=\" \" ind2=\" \">\n"
                            "    <subfield code=\"a\">%(url)s</subfield>\n"
                            "    <subfield code=\"t\">%(doctype)s</subfield>\n"
                            "    </datafield>"
                           ) % {'url': obj.extra_data["_result"]["pdf"],
                                'doctype': doctype}
            updated_xml = '<?xml version="1.0"?>\n' \
                          '<collection>\n<record>\n' + fulltext_xml + \
                          '</record>\n</collection>'

            new_dict_representation = convert_marcxml_to_bibfield(updated_xml)
            try:
                if isinstance(new_dict_representation["fft"], list):
                    for element in new_dict_representation["fft"]:
                        obj.data['fft'].append(element)
                else:
                    obj.data['fft'].append(new_dict_representation["fft"])
            except (KeyError, TypeError):
                obj.data['fft'] = [new_dict_representation['fft']]

            obj.add_task_result("filesfft", new_dict_representation["fft"])
    else:
        eng.log.info("There was already a pdf register for this record,"
                     "perhaps a duplicate task in you workflow.")


def quick_match_record(obj, eng):
    """
    Retrieve the record Id from a record by using tag 001 or SYSNO or OAI ID or DOI
    tag. opt_mod is the desired mode.

    001 fields even in the insert mode

    :param obj: Bibworkflow Object to process
    :param eng: BibWorkflowEngine processing the object

    """
    from invenio.legacy.bibupload.engine import (find_record_from_recid,
                                                 find_record_from_sysno,
                                                 find_records_from_extoaiid,
                                                 find_record_from_oaiid,
                                                 find_record_from_doi)

    function_dictionnary = {'recid': find_record_from_recid,
                            'system_number': find_record_from_sysno,
                            'oaiid': find_record_from_oaiid,
                            'system_number_external': find_records_from_extoaiid,
                            'doi': find_record_from_doi}

    identifiers = {}

    try:
        #get our persistent identifier safe easy way

        for key in function_dictionnary.keys():
            if key in obj.data:
                temp_result = obj.data[key]
                if isinstance(temp_result, dict):
                    temp_result = temp_result["value"]
                identifiers[key] = temp_result

        if not identifiers:
            return False
        else:
            obj.extra_data["persistent_ids"] = identifiers
    except KeyError:
        identifiers = {}
    if "recid" not in identifiers:
        for identifier in identifiers:
            recid = function_dictionnary[identifier](
                identifiers[identifier])
            print(str(recid))
            if recid:
                if 'recid' not in obj.data:
                    obj.data['recid'] = {'value': recid}
                else:
                    obj.data['recid']['value'] = recid
                obj.extra_data["persistent_ids"]["recid"] = recid
                return True
        return False
    else:
        return True


def upload_record(mode="ir"):
    """

    :param mode:
    :return:
    """

    def _upload_record(obj, eng):
        eng.log_info("Saving data to temporary file for upload")
        filename = obj.save_to_file()
        params = ["-%s" % (mode,), filename]
        task_id = bibtask.task_low_level_submission("bibupload", "bibworkflow",
                                                    *tuple(params))
        eng.log_info("Submitted task #%s" % (task_id,))
    return _upload_record


def plot_extract(plotextractor_types):
    """

    :param plotextractor_types:
    :return: :raise workflows_error.WorkflowError:
    """
    from invenio.utils.plotextractor.output_utils import (create_MARC,
                                                          create_contextfiles,
                                                          prepare_image_data,
                                                          remove_dups)
    from invenio.utils.plotextractor.cli import (get_defaults,
                                                 extract_captions,
                                                 extract_context)
    from invenio.utils.plotextractor.converter import convert_images

    def _plot_extract(obj, eng):
        """
        Performs the plotextraction step.
        """
        # Download tarball for each harvested/converted record,
        # then run plotextrator.
        # Update converted xml files with generated xml or add it for upload
        bibtask.task_sleep_now_if_required()
        if "_result" not in obj.extra_data:
            obj.extra_data["_result"] = {}

        if 'p_extraction-source' not in obj.extra_data["_repository"][
            "arguments"]:
            p_extraction_source = plotextractor_types
        else:
            p_extraction_source = obj.extra_data["_repository"]["arguments"][
                'p_extraction-source']

        if not isinstance(p_extraction_source, list):
            p_extraction_source = [p_extraction_source]

        if 'latex' in p_extraction_source:
            # Run LaTeX plotextractor
            if "tarball" not in obj.extra_data["_result"]:
                extract_path = plotextractor_getter.make_single_directory(
                    cfg['CFG_TMPSHAREDDIR'], eng.uuid)
                tarball, pdf = plotextractor_getter.harvest_single(
                    obj.data["system_number_external"]["value"], extract_path,
                    ["tarball"])
                tarball = str(tarball)
                if tarball is None:
                    raise workflows_error.WorkflowError(
                        str("Error harvesting tarball from id: %s %s" %
                            (obj.data["system_number_external"]["value"],
                             extract_path)),
                        eng.uuid,
                        id_object=obj.id)

                obj.extra_data["_result"]["tarball"] = tarball
            else:
                tarball = obj.extra_data["_result"]["tarball"]

            sub_dir, refno = get_defaults(tarball, cfg['CFG_TMPDIR'], "")

            tex_files = None
            image_list = None
            try:
                extracted_files_list, image_list, tex_files = untar(tarball,
                                                                    sub_dir)
            except Timeout:
                eng.log.error(
                    'Timeout during tarball extraction on %s' % (tarball,))

            converted_image_list = convert_images(image_list)
            eng.log.info('converted %d of %d images found for %s' % (
                len(converted_image_list),
                len(image_list),
                os.path.basename(tarball)))
            extracted_image_data = []
            if tex_files == [] or tex_files is None:
                eng.log.error(
                    '%s is not a tarball' % (os.path.split(tarball)[-1],))
                run_shell_command('rm -r %s', (sub_dir,))
            else:
                for tex_file in tex_files:
                    # Extract images, captions and labels
                    partly_extracted_image_data = extract_captions(tex_file,
                                                                   sub_dir,
                                                                   converted_image_list)
                    if partly_extracted_image_data:
                        # Add proper filepaths and do various cleaning
                        cleaned_image_data = prepare_image_data(
                            partly_extracted_image_data,
                            tex_file, converted_image_list)
                        # Using prev. extracted info, get contexts for each
                        # image found
                        extracted_image_data.extend(
                            (extract_context(tex_file, cleaned_image_data)))

            if extracted_image_data:
                extracted_image_data = remove_dups(extracted_image_data)
                create_contextfiles(extracted_image_data)
                marc_xml = '<?xml version="1.0" encoding="UTF-8"?>\n<collection>\n'
                marc_xml += create_MARC(extracted_image_data, tarball, None)
                marc_xml += "\n</collection>"

                if marc_xml:
                    # We store the path to the directory  the tarball
                    # contents live
                    # Read and grab MARCXML from plotextractor run
                    new_dict = convert_marcxml_to_bibfield(marc_xml)

                    try:
                        if isinstance(new_dict["fft"], list):
                            for element in new_dict["fft"]:
                                obj.data['fft'].append(element)
                        else:
                            obj.data['fft'].append(new_dict["fft"])

                    except KeyError:
                        obj.data['fft'] = [new_dict['fft']]
                    obj.add_task_result("filesfft", new_dict["fft"])
                    obj.add_task_result("number_picture_converted",
                                        len(converted_image_list))
                    obj.add_task_result("number_of_picture_total",
                                        len(image_list))

    return _plot_extract


def refextract(obj, eng):
    """
    Performs the reference extraction step.

    :param obj: Bibworkflow Object to process
    :param eng: BibWorkflowEngine processing the object
    """
    from invenio.legacy.refextract.api import extract_references_from_file_xml

    bibtask.task_sleep_now_if_required()
    if "_result" not in obj.extra_data:
        obj.extra_data["_result"] = {}
    if "pdf" not in obj.extra_data["_result"]:
        extract_path = plotextractor_getter.make_single_directory(
            cfg['CFG_TMPSHAREDDIR'], eng.uuid)
        tarball, pdf = plotextractor_getter.harvest_single(
            obj.data["system_number_external"]["value"], extract_path, ["pdf"])

        if pdf is not None:
            obj.extra_data["_result"]["pdf"] = pdf

    elif not os.path.isfile(obj.extra_data["_result"]["pdf"]):
        extract_path = plotextractor_getter.make_single_directory(
            cfg['CFG_TMPSHAREDDIR'], eng.uuid)
        tarball, pdf = plotextractor_getter.harvest_single(
            obj.data["system_number_external"]["value"], extract_path, ["pdf"])
        if pdf is not None:
            obj.extra_data["_result"]["pdf"] = pdf

    if os.path.isfile(obj.extra_data["_result"]["pdf"]):
        cmd_stdout = extract_references_from_file_xml(
            obj.extra_data["_result"]["pdf"])
        references_xml = REGEXP_REFS.search(cmd_stdout)
        if references_xml:
            updated_xml = '<?xml version="1.0" encoding="UTF-8"?>\n' \
                          '<collection>\n<record>' + references_xml.group(1) + \
                          "</record>\n</collection>"

            new_dict_representation = convert_marcxml_to_bibfield(updated_xml)
            try:
                obj.data['reference'].append(
                    new_dict_representation["reference"])
            except KeyError:
                if 'reference' in new_dict_representation:
                    obj.data['reference'] = [
                        new_dict_representation['reference']]
            obj.add_task_result("reference",
                                new_dict_representation['reference'])

    else:
        obj.log.error("Not able to download and process the PDF ")


def author_list(obj, eng):
    """
    Performs the special authorlist extraction step
    (Mostly INSPIRE/CERN related).

    :param obj: Bibworkflow Object to process
    :param eng: BibWorkflowEngine processing the object
    """
    from invenio.legacy.oaiharvest.utils import (translate_fieldvalues_from_latex,
                                                 find_matching_files)
    from invenio.legacy.bibrecord import create_records, record_xml_output
    from invenio.legacy.bibconvert.xslt_engine import convert
    from invenio.utils.plotextractor.cli import get_defaults

    identifiers = obj.data["system_number_external"]["value"]
    bibtask.task_sleep_now_if_required()
    if "_result" not in obj.extra_data:
        obj.extra_data["_result"] = {}
    if "tarball" not in obj.extra_data["_result"]:
        extract_path = plotextractor_getter.make_single_directory(
            cfg['CFG_TMPSHAREDDIR'], eng.uuid)
        tarball, pdf = plotextractor_getter.harvest_single(
            obj.data["system_number_external"]["value"], extract_path,
            ["tarball"])
        tarball = str(tarball)
        if tarball is None:
            raise workflows_error.WorkflowError(str(
                "Error harvesting tarball from id: %s %s" % (
                    identifiers, extract_path)),
                                                eng.uuid,
                                                id_object=obj.id)
        obj.extra_data["_result"]["tarball"] = tarball

    sub_dir, dummy = get_defaults(obj.extra_data["_result"]["tarball"],
                                  cfg['CFG_TMPDIR'], "")

    try:
        untar(obj.extra_data["_result"]["tarball"], sub_dir)
    except Timeout:
        eng.log.error('Timeout during tarball extraction on %s' % (
            obj.extra_data["_result"]["tarball"]))

    xml_files_list = find_matching_files(sub_dir, ["xml"])

    authors = ""

    for xml_file in xml_files_list:
        xml_file_fd = open(xml_file, "r")
        xml_content = xml_file_fd.read()
        xml_file_fd.close()

        match = REGEXP_AUTHLIST.findall(xml_content)
        if not match == []:
            authors += match[0]
            # Generate file to store conversion results
    if authors is not '':
        authors = convert(authors, "authorlist2marcxml.xsl")
        authorlist_record = create_records(authors)
        if len(authorlist_record) == 1:
            if authorlist_record[0][0] is None:
                eng.log.error("Error parsing authorlist record for id: %s" % (
                    identifiers,))
            authorlist_record = authorlist_record[0][0]
            # Convert any LaTeX symbols in authornames
        translate_fieldvalues_from_latex(authorlist_record, '100', code='a')
        translate_fieldvalues_from_latex(authorlist_record, '700', code='a')

        updated_xml = '<?xml version="1.0" encoding="UTF-8"?>\n<collection>\n' + record_xml_output(
            authorlist_record) \
                      + '</collection>'
        if not None == updated_xml:
            # We store the path to the directory  the tarball contents live
            # Read and grab MARCXML from plotextractor run
            new_dict_representation = convert_marcxml_to_bibfield(updated_xml)
            obj.data['authors'] = new_dict_representation["authors"]
            obj.data['number_of_authors'] = new_dict_representation[
                "number_of_authors"]
            obj.add_task_result("authors", new_dict_representation["authors"])
            obj.add_task_result("number_of_authors",
                                new_dict_representation["number_of_authors"])


def upload_step(obj, eng):
    """
    Perform the upload step.

    :param obj: Bibworkflow Object to process
    :param eng: BibWorkflowEngine processing the object
    """
    from invenio.legacy.oaiharvest.dblayer import create_oaiharvest_log_str

    uploaded_task_ids = []
    #Work comment:
    #
    #Prepare in case of filtering the files to up,
    #no filtering, no other things to do
    marcxml_value = obj.data.legacy_export_as_marc()
    task_id = None
    # Get a random sequence ID that will allow for the tasks to be
    # run in order, regardless if parallel task execution is activated
    sequence_id = random.randrange(1, 60000)
    bibtask.task_sleep_now_if_required()
    extract_path = plotextractor_getter.make_single_directory(
        cfg['CFG_TMPSHAREDDIR'], eng.uuid)
    # Now we launch BibUpload tasks for the final MARCXML files
    filepath = extract_path + os.sep + str(obj.id)
    file_fd = open(filepath, 'w')
    file_fd.write(marcxml_value)
    file_fd.close()
    mode = ["-r", "-i"]

    arguments = obj.extra_data["_repository"]["arguments"]

    if os.path.exists(filepath):
        try:
            args = mode
            if sequence_id:
                args.extend(['-I', str(sequence_id)])
            if arguments.get('u_name', ""):
                args.extend(['-N', arguments.get('u_name', "")])
            if arguments.get('u_priority', 5):
                args.extend(['-P', str(arguments.get('u_priority', 5))])
            args.append(filepath)
            task_id = bibtask.task_low_level_submission("bibupload",
                                                        "oaiharvest",
                                                        *tuple(args))
            create_oaiharvest_log_str(task_id,
                                      obj.extra_data["_repository"]["id"],
                                      marcxml_value)
        except Exception as msg:
            eng.log.error(
                "An exception during submitting oaiharvest task occured : %s " % (
                    str(msg)))
            return None
    else:
        eng.log.error("marcxmlfile %s does not exist" % (filepath,))
    if task_id is None:
        eng.log.error("an error occurred while uploading %s from %s" %
                      (filepath, obj.extra_data["_repository"]["name"]))
    else:
        uploaded_task_ids.append(task_id)
        eng.log.info(
            "material harvested from source %s was successfully uploaded" %
            (obj.extra_data["_repository"]["name"],))
    if cfg['CFG_INSPIRE_SITE']:
        # Launch BibIndex,Webcoll update task to show uploaded content quickly
        bibindex_params = ['-w', 'collection,reportnumber,global',
                           '-P', '6',
                           '-I', str(sequence_id),
                           '--post-process',
                           'bst_run_bibtask[taskname="webcoll", user="oaiharvest", P="6", c="HEP"]']
        bibtask.task_low_level_submission("bibindex", "oaiharvest",
                                          *tuple(bibindex_params))
    eng.log.info("end of upload")


def bibclassify(taxonomy, rebuild_cache=False, no_cache=False,
                output_mode='text',
                output_limit=20, spires=False, match_mode='full',
                with_author_keywords=False,
                extract_acronyms=False, only_core_tags=False):
    """

    :param taxonomy:
    :param rebuild_cache:
    :param no_cache:
    :param output_mode:
    :param output_limit:
    :param spires:
    :param match_mode:
    :param with_author_keywords:
    :param extract_acronyms:
    :param only_core_tags:
    :return:
    """

    def _bibclassify(obj, eng):
        import os.path

        if not os.path.isfile(taxonomy):
            eng.log.error("No RDF found, no bibclassify can run")
            return None

        from invenio.legacy.bibclassify import api

        if "_result" not in obj.extra_data:
            obj.extra_data["_result"] = {}

        if "pdf" in obj.extra_data["_result"]:
            obj.extra_data["_result"][
                "bibclassify"] = api.bibclassify_exhaustive_call(
                obj.extra_data["_result"]["pdf"],
                taxonomy, rebuild_cache,
                no_cache,
                output_mode, output_limit,
                spires,
                match_mode, with_author_keywords,
                extract_acronyms, only_core_tags
            )
            obj.add_task_result("bibclassify",
                                obj.extra_data["_result"]["bibclassify"])
        else:
            obj.log.error("No classification done due to missing fulltext."
                          "\n You need to get it before! see fulltext task")

    return _bibclassify
