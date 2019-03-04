# -*- coding: utf-8 -*-
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
This module provides functionality for automation of releasing projects
into various downstream services
"""
import logging
import re
import time
from semantic_version import Version, validate
from sys import exit

from release_bot.cli import CLI
from release_bot.configuration import configuration
from release_bot.exceptions import ReleaseException
from release_bot.fedora import Fedora
from release_bot.git import Git
from release_bot.github import Github
from release_bot.pypi import PyPi


class ReleaseBot:

    def __init__(self, configuration):
        self.conf = configuration
        url = f'https://github.com/{self.conf.repository_owner}/{self.conf.repository_name}.git'
        self.git = Git(url, self.conf)
        self.github = Github(configuration, self.git)
        self.pypi = PyPi(configuration, self.git)
        self.fedora = Fedora(configuration)
        self.logger = configuration.logger
        # FIXME: it's cumbersome to work with these dicts - it's unclear how the content changes;
        #        get rid of them and replace them with individual variables
        self.new_release = {}
        self.new_pr = {}

    def cleanup(self):
        if 'tempdir' in self.new_release:
            self.new_release['tempdir'].cleanup()
        self.new_release = {}
        self.new_pr = {}
        self.github.comment = []
        self.fedora.progress_log = []
        self.git.cleanup()

    def load_release_conf(self):
        """
        Updates new_release with latest release-conf.yaml from repository
        :return:
        """
        # load release configuration from release-conf.yaml in repository
        conf = self.github.get_configuration()
        release_conf = self.conf.load_release_conf(conf)
        self.new_release.update(release_conf)

    def find_open_release_issues(self):
        """
        Looks for opened release issues on github
        :return: True on found, False if not found
        """
        cursor = ''
        release_issues = {}
        while True:
            edges = self.github.walk_through_open_issues(start=cursor, direction='before')
            if not edges:
                self.logger.debug(f'No more open issues found')
                break
            else:
                for edge in reversed(edges):
                    cursor = edge['cursor']
                    last_version = Version(self.github.latest_release())
                    version = ''
                    match = False
                    re_match = re.match(r'(.+) release', edge['node']['title'].lower())
                    if (re_match):
                        match = True
                        version = re_match[1].strip()
                    if (edge['node']['title'].lower().strip() == "new major release"):
                        match = True
                        version = str(last_version.next_major())
                    elif (edge['node']['title'].lower().strip() == "new minor release"):
                        match = True
                        version = str(last_version.next_minor())
                    elif (edge['node']['title'].lower().strip() == "new patch release"):
                        match = True
                        version = str(last_version.next_patch())

                    if match:
                        if validate(version):
                            if edge['node']['authorAssociation'] in ['MEMBER', 'OWNER',
                                                                     'COLLABORATOR']:
                                release_issues[version] = edge['node']
                                self.logger.info(f'Found new release issue with version: {version}')
                            else:
                                self.logger.warning(
                                    f"Author association {edge['node']['authorAssociation']!r} "
                                    f"not in ['MEMBER', 'OWNER', 'COLLABORATOR']")
                        else:
                            self.logger.warning(f"{version!r} is not a valid version")
        if len(release_issues) > 1:
            msg = f'Multiple release issues are open {release_issues}, please reduce them to one'
            self.logger.error(msg)
            return False
        if len(release_issues) == 1:
            for version, node in release_issues.items():
                self.new_pr = {'version': version,
                               'issue_id': node['id'],
                               'issue_number': node['number'],
                               'labels': self.new_release.get('labels')}
                return True
        else:
            return False

    def find_newest_release_pull_request(self):
        """
        Find newest merged release PR

        :return: bool, whether PR was found
        """
        cursor = ''
        while True:
            edges = self.github.walk_through_prs(start=cursor, direction='before', closed=True)
            if not edges:
                self.logger.debug(f'No merged release PR found')
                return False

            for edge in reversed(edges):
                cursor = edge['cursor']
                last_version = Version(self.github.latest_release())
                version = ''
                match = False
                re_match = re.match(r'(.+) release', edge['node']['title'].lower())
                if (re_match):
                    match = True
                    version = re_match[1].strip()
                if (edge['node']['title'].lower().strip() == "new major release"):
                    match = True
                    version = str(last_version.next_major())
                elif (edge['node']['title'].lower().strip() == "new minor release"):
                    match = True
                    version = str(last_version.next_minor())
                elif (edge['node']['title'].lower().strip() == "new patch release"):
                    match = True
                    version = str(last_version.next_patch())

                if match and validate(version):
                    merge_commit = edge['node']['mergeCommit']
                    self.logger.info(f"Found merged release PR with version {version}, "
                                     f"commit id: {merge_commit['oid']}")
                    new_release = {'version': version,
                                   'commitish': merge_commit['oid'],
                                   'pr_id': edge['node']['id'],
                                   'author_name': merge_commit['author']['name'],
                                   'author_email': merge_commit['author']['email']}
                    self.new_release.update(new_release)
                    return True

    def make_release_pull_request(self):
        """
        Makes release pull request and handles outcome
        :return: whether making PR was successful
        """

        def pr_handler(success):
            """
            Handler for the outcome of making a PR
            :param success: whether making PR was successful
            :return:
            """
            result = 'made' if success else 'failed to make'
            msg = f"I just {result} a PR request for a release version {self.new_pr['version']}"
            level = logging.INFO if success else logging.ERROR
            self.logger.log(level, msg)
            if success:
                msg += f"\n Here's a [link to the PR]({self.new_pr['pr_url']})"
            comment_backup = self.github.comment.copy()
            self.github.comment = [msg]
            self.github.add_comment(self.new_pr['issue_id'])
            self.github.comment = comment_backup
            if success:
                self.github.close_issue(self.new_pr['issue_number'])

        latest_gh_str = self.github.latest_release()
        self.new_pr['previous_version'] = latest_gh_str
        if Version.coerce(latest_gh_str) >= Version.coerce(self.new_pr['version']):
            msg = f"Version ({latest_gh_str}) is already released and this issue is ignored."
            self.logger.warning(msg)
            return False
        msg = f"Making a new PR for release of version {self.new_pr['version']} based on an issue."
        self.logger.info(msg)

        try:
            self.new_pr['repo'] = self.git
            if not self.new_pr['repo']:
                raise ReleaseException("Couldn't clone repository!")

            if self.github.make_release_pr(self.new_pr):
                pr_handler(success=True)
                return True
        except ReleaseException:
            pr_handler(success=False)
            raise
        return False

    def make_new_github_release(self):
        def release_handler(success):
            result = "released" if success else "failed to release"
            msg = f"I just {result} version {self.new_release['version']} on Github"
            level = logging.INFO if success else logging.ERROR
            self.logger.log(level, msg)
            self.github.comment.append(msg)

        try:
            latest_release = self.github.latest_release()
        except ReleaseException as exc:
            raise ReleaseException(f"Failed getting latest Github release (zip).\n{exc}")

        if Version.coerce(latest_release) >= Version.coerce(self.new_release['version']):
            self.logger.info(
                f"{self.new_release['version']} has already been released on Github")
        else:
            try:
                released, self.new_release = self.github.make_new_release(self.new_release)
                if released:
                    release_handler(success=True)
            except ReleaseException:
                release_handler(success=False)
                raise
        self.github.update_changelog(self.new_release['version'])
        return self.new_release

    def make_new_pypi_release(self):
        def release_handler(success):
            result = "released" if success else "failed to release"
            msg = f"I just {result} version {self.new_release['version']} on PyPI"
            level = logging.INFO if success else logging.ERROR
            self.logger.log(level, msg)
            self.github.comment.append(msg)

        latest_pypi = self.pypi.latest_version()
        if Version.coerce(latest_pypi) >= Version.coerce(self.new_release['version']):
            self.logger.info(f"{self.new_release['version']} has already been released on PyPi")
            return False
        self.git.fetch_tags()
        self.git.checkout(self.new_release['version'])
        try:
            self.pypi.release()
            release_handler(success=True)
        except ReleaseException:
            release_handler(success=False)
            raise

        return True

    def make_new_fedora_release(self):
        if not self.new_release.get('fedora'):
            self.logger.debug('Skipping Fedora release')
            return

        self.logger.info("Triggering Fedora release")

        def release_handler(success):
            result = "released" if success else "failed to release"
            msg = f"I just {result} on Fedora"
            builds = ', '.join(self.fedora.builds)
            bodhi_update_url = "https://bodhi.fedoraproject.org/updates/new"
            if builds:
                msg += f", successfully built for branches: {builds}."
                msg += f" Follow this link to create bodhi update(s): {bodhi_update_url}"
            level = logging.INFO if success else logging.ERROR
            self.logger.log(level, msg)
            self.github.comment.append(msg)

        try:
            name, email = self.github.get_user_contact()
            self.new_release['commit_name'] = name
            self.new_release['commit_email'] = email
            success_ = self.fedora.release(self.new_release)
            release_handler(success_)
        except ReleaseException:
            release_handler(success=False)
            raise

    def run(self):
        self.logger.info(f"release-bot v{configuration.version} reporting for duty!")
        try:
            while True:
                self.git.pull()
                try:
                    self.load_release_conf()
                    if self.find_newest_release_pull_request():
                        self.make_new_github_release()
                        # Try to do PyPi release regardless whether we just did github release
                        # for case that in previous iteration (of the 'while True' loop)
                        # we succeeded with github release, but failed with PyPi release
                        if self.make_new_pypi_release():
                            # There's no way how to tell whether there's already such a fedora 'release'
                            # so try to do it only when we just did PyPi release
                            self.make_new_fedora_release()
                    if self.new_release.get('trigger_on_issue') and self.find_open_release_issues():
                        if self.new_release.get('labels') is not None:
                            self.github.put_labels_on_issue(self.new_pr['issue_number'],
                                                            self.new_release.get('labels'))
                        self.make_release_pull_request()
                except ReleaseException as exc:
                    self.logger.error(exc)

                self.github.add_comment(self.new_release.get('pr_id'))
                self.logger.debug(f"Done. Going to sleep for {self.conf.refresh_interval}s")
                time.sleep(self.conf.refresh_interval)
        finally:
            self.cleanup()


def main():
    CLI.parse_arguments()
    configuration.load_configuration()
    rb = ReleaseBot(configuration)
    #rb.find_open_release_issues()
    rb.run()


if __name__ == '__main__':
    exit(main())
