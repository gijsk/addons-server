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
import os
import random
import re
import time

from locust import HttpLocust, TaskSet, task
import lxml.html

logging.Formatter.converter = time.gmtime

log = logging.getLogger(__name__)

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


class UserBehavior(TaskSet):

    @task(1)
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


class WebsiteUser(HttpLocust):
    task_set = UserBehavior
    min_wait = 5000
    max_wait = 9000
