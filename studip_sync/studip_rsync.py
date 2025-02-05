from datetime import datetime
import os
import shutil
import tempfile
import time
import unicodedata
import string
import re

from studip_sync.arg_parser import ARGS
from studip_sync.config import CONFIG
from studip_sync.logins import LoginError
from studip_sync.plugins.plugins import PLUGINS
from studip_sync.session import Session, DownloadError, MissingFeatureError, \
    MissingPermissionFolderError
from studip_sync.parsers import ParserError


class StudIPRSync(object):

    def __init__(self):
        super(StudIPRSync, self).__init__()
        self.workdir = tempfile.mkdtemp(prefix="studip-sync")
        self.files_destination_dir = CONFIG.files_destination
        self.media_destination_dir = CONFIG.media_destination
        self.ignore_courses = CONFIG.ignore_courses

        if self.files_destination_dir:
            os.makedirs(self.files_destination_dir, exist_ok=True)
        if self.media_destination_dir:
            os.makedirs(self.media_destination_dir, exist_ok=True)

    def sync(self, sync_fully=False, sync_recent=False, use_api=True):
        PLUGINS.hook("hook_start")

        with Session(base_url=CONFIG.base_url, plugins=PLUGINS) as session:
            print("Logging in...")
            try:
                session.login(CONFIG.auth_type, CONFIG.auth_type_data, CONFIG.username,
                              CONFIG.password)
            except (LoginError, ParserError) as e:
                print("Login failed!")
                print(e)
                return 1

            print("Changing semester visibility...")
            if CONFIG.semester is not None:
                if CONFIG.semester in ["all", "current", "last", "future", "lastandnext", "lastandbefore"]:
                    session.set_semester(CONFIG.semester)
                else:
                    semester_id = session.get_semester_id(CONFIG.semester)
                    if semester_id is None:
                        print("Semester not found!")
                        return 1
                    session.set_semester()
            elif CONFIG.use_new_file_structure:
                session.set_semester("all")

            print("Downloading course list...")

            try:
                courses = list(session.get_courses(sync_recent))
            except (LoginError, ParserError) as e:
                print("Downloading course list failed!")
                print(e)
                return 1

            if sync_recent:
                print("Syncing only the most recent semester!")

            status_code = 0
            for i in range(0, len(courses)):
                course = courses[i]
                if course["course_id"] in self.ignore_courses or \
                    any(re.match(ignore.replace('*', '.*'), course["save_as"]) for ignore in self.ignore_courses):
                    print(f"Skipping course \"{course['save_as']}\" as it is in the ignore list.")
                    continue
                print("{}) {}: {}".format(i + 1, course["semester"], course["save_as"]))

                course_save_as = get_course_save_as(course)

                if self.files_destination_dir:
                    try:
                        files_root_dir = os.path.join(self.files_destination_dir, course_save_as)

                        CourseRSync(session, self.workdir, files_root_dir, course,
                                    sync_fully, use_api).download()
                    except MissingFeatureError:
                        # Ignore if there are no files
                        pass
                    except DownloadError as e:
                        print("\tDownload of files failed: " + str(e))
                        status_code = 2
                        raise e

                if self.media_destination_dir:
                    try:
                        print("\tSyncing media files...")

                        media_root_dir = os.path.join(self.media_destination_dir,
                                                      course_save_as)

                        session.download_media(course["course_id"], media_root_dir,
                                               course["save_as"])
                    except MissingFeatureError:
                        # Ignore if there is no media
                        pass
                    except MissingPermissionFolderError:
                        # Ignore if there are no permissions
                        pass
                    except DownloadError as e:
                        print("\tDownload of media failed: " + str(e))
                        status_code = 2
                        raise e
                    except ParserError as e:
                        print("\tDownload of media failed: " + str(e))
                        if status_code != 0:
                            raise e
                        else:
                            status_code = 2
            
            print("Changing semester visibility back to current...")
            session.set_semester("current")

        if self.files_destination_dir and status_code == 0:
            CONFIG.update_last_sync(int(time.time()))

        return status_code

    def cleanup(self):
        shutil.rmtree(self.workdir)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.cleanup()


UNICODE_NORMALIZE_MODE = "NFKC"

def clean_name(name):
    # Remove all disallowed characters for Windows, Mac and Linux
    return re.sub(r'[\\:*?"<>|]', '', name.replace("/", "--")).strip()

def short_course_name(name):
    # Remove all non-alphanumeric symbols
    #clean_name = re.sub(r'[^a-zA-ZÄÖÜ0-9\s]', '', name)
    # pattern: <number> <type letter> <course name (max 2)> <optional digit>
    pattern = re.compile(r'(\d+)\s([A-ZÄÖÜ])\S*\s(([A-Z]+[a-zäöüß]* ?){1,2})\D*(\d?).*')
    match = pattern.match(clean_name(name))
    if match:
        res = f"{match.group(1)} {match.group(2)} {match.group(3)}{match.group(5)}".strip()
        print("Result: "+res)
        return res
    else:
        return clean_name(name)

def check_and_cleanup_form_data(form_data_files, form_data_folders, use_api):
    form_data_files_new = []
    for form_data in form_data_files:
        try:
            if "id" not in form_data:
                log("Skipped file that can't be downloaded: {}".format(form_data["name"]))
                continue
                
            form_id = form_data["id"]
            
            if not all(c in string.hexdigits for c in form_id):
                raise ParserError("id is not hexadecimal")

            # TODO: support links by saving them as .url files
            if "size" not in form_data or form_data["size"] is None or ("storage" in form_data and form_data["storage"] == "url") or ("icon" in form_data and form_data["icon"] == "link-extern"):
                if ARGS.v:
                    print("[Debug] " + str(form_data))
                log("Found unsupported file: {}".format(form_data["name"]))
                continue

            if use_api and "is_downloadable" in form_data and not form_data["is_downloadable"]:
                log("Skipped file that can't be downloaded: {}".format(form_data["name"]))
                continue

            new_file_data = {
                "name": clean_name(unicodedata.normalize(UNICODE_NORMALIZE_MODE, form_data["name"])),
                "id": form_id,
                "size": int(form_data["size"]),
                "chdate": int(form_data["chdate"])
            }

            if not use_api:
                new_file_data["download_url"] = form_data["download_url"]

            form_data_files_new.append(new_file_data)
        except Exception as e:
            print(form_data)
            raise ParserError("File attributes are invalid: {}".format(e))

    form_data_folders_new = []
    for form_data in form_data_folders:
        try:
            if "id" not in form_data:
                log("Skipped folder that can't be downloaded")
                continue
            form_id = form_data["id"]
            if not all(c in string.hexdigits for c in form_id):
                raise ValueError("id is not hexadecimal")

            form_data_folders_new.append({
                "name": clean_name(unicodedata.normalize(UNICODE_NORMALIZE_MODE, form_data["name"])),
                "id": form_id
            })
        except Exception as e:
            print(form_data)
            raise ParserError("Folder attributes are invalid: {}".format(e))

    return form_data_files_new, form_data_folders_new


def log(message, flush=False):
    if flush:
        print("\t\t" + message, end="\r", flush=True)
    else:
        print("\t\t" + message)


def is_file_new(file, file_path):
    if not file["size"]:
        # If there is no size, skip this file, since it cant be downloaded
        return False


    if not os.path.exists(file_path):
        log("File changed: new: {}".format(file_path))
        return True

    file_time = int(os.path.getmtime(file_path))

    chdate = file["chdate"]
    if chdate > file_time:
        log("File changed: time: {} - {} : {}".format(chdate, file_time, file_path))
        return True

    file_size = os.path.getsize(file_path)

    size = file["size"]
    if not size == file_size:
        log("File changed: size: {} - {} : {}".format(size, file_size, file_path))
        return True

    return False


def get_course_save_as(course):
    if CONFIG.use_new_file_structure:
        save_as_semester = clean_name(course["semester"])
        save_as_semester = "{} - {}".format(course["semester_id"], save_as_semester)

        return os.path.join(save_as_semester, short_course_name(course["save_as"]))
    else:
        return short_course_name(course["save_as"])


class CourseRSync:

    def __init__(self, session, workdir, root_folder, course, sync_fully, use_api):
        self.session = session
        self.workdir = workdir
        self.course_id = course["course_id"]
        self.course_save_as = course["save_as"]
        self.root_folder = root_folder
        self.sync_fully = sync_fully
        self.use_api = use_api

    def download(self):
        if self.course_has_new_files(self.sync_fully):
            print("\tSyncing files...")
            self.download_recursive()
        else:
            print("\tSkipping this course...")

    def course_has_new_files(self, sync_fully=False):
        if sync_fully:
            return True

        return self.session.check_course_new_files(self.course_id, CONFIG.last_sync)

    def download_recursive(self, folder_id=None, folder_path_relative=""):
        try:
            if self.use_api:
                form_data_files, form_data_folders = self.session.get_files_index_from_api(self.course_id,
                                                                              folder_id)
            else:
                form_data_files, form_data_folders = self.session.get_files_index(self.course_id,
                                                                              folder_id)
        except MissingPermissionFolderError:
            log("Couldn't view the following folder because of missing permissions: " + folder_path_relative)
            return

        form_data_files, form_data_folders = check_and_cleanup_form_data(form_data_files,
                                                                         form_data_folders, self.use_api)

        for file_data in form_data_files:
            if file_data["download_url"] is None:
                log("Skipped file that can't be downloaded: {}".format(file_data["name"]))
                continue
            folder_absolute = os.path.join(self.root_folder, folder_path_relative)
            file_path = os.path.join(folder_absolute, file_data["name"])
            if is_file_new(file_data, file_path):
                log("Downloading: {}: {}".format(file_data["id"], file_data["name"]))

                target_file = os.path.join(self.workdir, file_data["id"])

                if not self.use_api:
                    self.session.download_file(file_data["download_url"], target_file)
                else:
                    self.session.download_file_api(file_data["id"], target_file)

                file_size = int(file_data["size"])
                target_file_size = os.path.getsize(target_file)
                if target_file_size != file_size:
                    if ARGS.v:
                        print("[Debug] " + str(form_data_files))
                    raise DownloadError("File size didn't match expected file size: " + file_path)

                file_path_base, file_path_name = os.path.split(file_path)
                if os.path.exists(file_path):
                    timestr = datetime.strftime(datetime.now(), "%Y-%m-%d_%H+%M+%S")
                    suffix = "_" + timestr + ".old"
                    new_file_path = os.path.join(file_path_base, file_path_name + suffix)
                    os.rename(file_path, new_file_path)
                else:
                    os.makedirs(file_path_base, exist_ok=True)

                if os.path.exists(file_path):
                    raise DownloadError("File exists already, even after moving it away: " +
                                        file_path)

                shutil.copyfile(target_file, file_path)

                self.session.plugins.hook("hook_file_download_successful", file_data["name"],
                                          self.course_save_as, file_path)

        for folder_data in form_data_folders:
            new_folder_path_relative = os.path.join(folder_path_relative, folder_data["name"])

            # self.log("Accessing folder: " + folder_data["id"] + ": " + folder_data["name"])
            self.download_recursive(folder_data["id"], new_folder_path_relative)
