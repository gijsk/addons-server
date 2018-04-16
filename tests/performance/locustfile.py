"""
This module causes logging of test start/stop and other critical events.

The locust logging format is not necessarily stable, so we use the event hooks
API to implement our own "stable" logging for later programmatic reference.

The events are:

* locust_start_hatching
* master_start_hatching
* quitting
* hatch_complete
"""
import logging
import random
import time
import collections
import os
import string
import re
import tempfile
import uuid
from contextlib import contextmanager
from shutil import make_archive, rmtree
from zipfile import ZipFile

from locust import HttpLocust, TaskSet, task
import lxml.html
from lxml.html import submit_form

from fxa.constants import ENVIRONMENT_URLS
from fxa.core import Client
from fxa.tests.utils import TestEmailAccount

logging.Formatter.converter = time.gmtime

log = logging.getLogger(__name__)

MAX_UPLOAD_POLL_ATTEMPTS = 200
ID_REGEX = re.compile('THIS_IS_THE_ID')
NAME_REGEX = re.compile('THIS_IS_THE_NAME')

root_path = os.path.dirname(__file__)
data_dir = os.path.join(root_path, 'fixtures')
xpis = [os.path.join(data_dir, xpi) for xpi in os.listdir(data_dir)]
log = logging.getLogger(__name__)


def get_random():
    return str(uuid.uuid4())


def submit_url(step):
    return '/en-US/developers/addon/submit/{step}'.format(step=step)


def get_xpi():
    return uniqueify_xpi(random.choice(xpis))


@contextmanager
def uniqueify_xpi(path):
    output_dir = tempfile.mkdtemp()
    try:
        data_dir = os.path.join(output_dir, 'xpi')
        output_path = os.path.join(output_dir, 'addon')
        xpi_name = os.path.basename(path)
        xpi_path = os.path.join(output_dir, xpi_name)
        with ZipFile(path) as original:
            original.extractall(data_dir)
        with open(os.path.join(data_dir, 'install.rdf')) as f:
            install_rdf = f.read()
        install_rdf = ID_REGEX.sub('{%s}' % get_random(), install_rdf)
        install_rdf = NAME_REGEX.sub(get_random(), install_rdf)
        with open(os.path.join(data_dir, 'install.rdf'), 'w') as f:
            f.write(install_rdf)
        archive_path = make_archive(output_path, 'zip', data_dir)
        os.rename(archive_path, xpi_path)
        with open(xpi_path) as f:
            yield f
    finally:
        rmtree(output_dir)


class EventMarker(object):
    """
    Simple event marker that logs on every call.
    """
    def __init__(self, name):
        self.name = name

    def _generate_log_message(self):
        log.info('locust event: {}'.format(self.name))

    def __call__(self, *args, **kwargs):
        self._generate_log_message()


def install_event_markers():
    # "import locust" within this scope so that this module is importable by
    # code running in environments which do not have locust installed.
    import locust

    # install simple event markers
    locust.events.locust_start_hatching += EventMarker('locust_start_hatching')
    locust.events.master_start_hatching += EventMarker('master_start_hatching')
    locust.events.quitting += EventMarker('quitting')
    locust.events.hatch_complete += EventMarker('hatch_complete')


install_event_markers()


def get_fxa_client():
    fxa_env = os.getenv('FXA_ENV', 'stage')
    return Client(ENVIRONMENT_URLS[fxa_env]['authentication'])


def get_fxa_account():
    fxa_client = get_fxa_client()

    account = TestEmailAccount()
    password = ''.join([random.choice(string.ascii_letters) for i in range(8)])
    FxAccount = collections.namedtuple('FxAccount', 'email password')
    fxa_account = FxAccount(email=account.email, password=password)
    session = fxa_client.create_account(fxa_account.email,
                                        fxa_account.password)
    account.fetch()
    message = account.wait_for_email(lambda m: 'x-verify-code' in m['headers'])
    session.verify_email_code(message['headers']['x-verify-code'])
    return fxa_account, account


def destroy_fxa_account(fxa_account, email_account):
    email_account.clear()
    get_fxa_client().destroy_account(fxa_account.email, fxa_account.password)


class UserBehavior(TaskSet):

    def on_start(self):
        self.fxa_account, self.email_account = get_fxa_account()
        log.info(
            'Created {account} for load-tests'
            .format(account=self.fxa_account))

    def on_stop(self):
        log.info(
            'Cleaning up and destroying {account}'
            .format(account=self.fxa_account))
        destroy_fxa_account(self.fxa_account, self.email_account)

    def submit_form(self, form=None, url=None, extra_values=None):
        if form is None:
            raise ValueError('form cannot be None; url={}'.format(url))

        def submit(method, form_action_url, values):
            values = dict(values)
            if 'csrfmiddlewaretoken' not in values:
                raise ValueError(
                    'Possibly the wrong form. Could not find '
                    'csrfmiddlewaretoken: {}'.format(repr(values)))
            with self.client.post(
                    url or form_action_url, values,
                    allow_redirects=False, catch_response=True) as response:
                if response.status_code not in (301, 302):
                    # This probably means the form failed and is displaying
                    # errors.
                    # TODO: scrape out the errors.
                    response.failure(
                        'Form submission did not redirect; status={}'
                        .format(response.status_code))

        submit_form(form, open_http=submit, extra_values=extra_values)

    def get_the_only_form_without_id(self, response_content):
        """
        Gets the only form on the page that doesn't have an ID.

        A lot of pages (login, registration) have a single form without an ID.
        This is the one we want. The other forms on the page have IDs so we
        can ignore them. I'm sure this will break one day.
        """
        html = lxml.html.fromstring(response_content)
        target_form = None
        for form in html.forms:
            if not form.attrib.get('id'):
                target_form = form
        if target_form is None:
            raise ValueError(
                'Could not find only one form without an ID; found: {}'
                .format(html.forms))
        return target_form

    def login(self, email, password):
        login_url = "/en-US/firefox/users/login"
        resp = self.client.get(login_url)
        login_form = self.get_the_only_form_without_id(resp.content)

        self.submit_form(
            form=login_form, url=login_url, extra_values={
                "username": email,
                "password": password})

    def load_upload_form(self):
        url = submit_url(2)
        with self.client.get(
                url, allow_redirects=False, catch_response=True) as response:
            if response.status_code == 200:
                html = lxml.html.fromstring(response.content)
                return html.get_element_by_id('create-addon')
            else:
                more_info = ''
                if response.status_code in (301, 302):
                    more_info = ('Location: {}'
                                 .format(response.headers['Location']))
                response.failure('Unexpected status: {}; {}'
                                 .format(response.status_code, more_info))

    def upload_addon(self, form):
        url = submit_url(2)
        csrfmiddlewaretoken = form.fields['csrfmiddlewaretoken']
        with get_xpi() as addon_file:
            with self.client.post(
                    '/en-US/developers/upload',
                    {'csrfmiddlewaretoken': csrfmiddlewaretoken},
                    files={'upload': addon_file},
                    name='/en-US/developers/upload ({})'.format(
                        os.path.basename(addon_file.name)),
                    allow_redirects=False,
                    catch_response=True) as response:
                if response.status_code == 302:
                    poll_url = response.headers['location']
                    upload_uuid = self.poll_upload_until_ready(poll_url)
                    if upload_uuid:
                        form.fields['upload'] = upload_uuid
                        self.submit_form(form=form, url=url)
                else:
                    response.failure('Unexpected status: {}'.format(
                        response.status_code))

    @task(1)
    def upload(self):
        form = self.load_upload_form()
        if form:
            self.upload_addon(form)

    @task(5)
    def browse(self):
        self.client.get('/en-US/firefox/')
        self.client.get('/en-US/firefox/search/?q=pi&appver=45.0&platform=mac')
        with self.client.get(
                '/en-US/firefox/extensions/',
                allow_redirects=False, catch_response=True) as response:
            if response.status_code == 200:
                html = lxml.html.fromstring(response.content)
                addon_links = html.cssselect('.item.addon h3 a')
                url = random.choice(addon_links).get('href')
                self.client.get(url, name='/en-US/firefox/addon/:slug')
            else:
                response.failure('Unexpected status code {}'.format(
                    response.status_code))

    def poll_upload_until_ready(self, url):
        for i in xrange(MAX_UPLOAD_POLL_ATTEMPTS):
            with self.client.get(url, allow_redirects=False,
                                 name='/en-US/developers/upload/:uuid/json',
                                 catch_response=True) as response:
                try:
                    data = response.json()
                except ValueError:
                    return response.failure(
                        'Failed to parse JSON when polling. '
                        'Status: {} content: {}'.format(
                            response.status_code, response.content))
                if response.status_code == 200:
                    if data['error']:
                        return response.failure('Unexpected error: {}'.format(
                            data['error']))
                    elif data['validation']:
                        return data['upload']
                else:
                    return response.failure('Unexpected status: {}'.format(
                        response.status_code))
                time.sleep(1)
        else:
            response.failure('Upload did not complete in {} tries'.format(
                MAX_UPLOAD_POLL_ATTEMPTS))


class WebsiteUser(HttpLocust):
    task_set = UserBehavior
    min_wait = 5000
    max_wait = 9000
