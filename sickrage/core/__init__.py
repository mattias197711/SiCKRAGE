# ##############################################################################
#  Author: echel0n <echel0n@sickrage.ca>
#  URL: https://sickrage.ca/
#  Git: https://git.sickrage.ca/SiCKRAGE/sickrage.git
#  -
#  This file is part of SiCKRAGE.
#  -
#  SiCKRAGE is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#  -
#  SiCKRAGE is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#  -
#  You should have received a copy of the GNU General Public License
#  along with SiCKRAGE.  If not, see <http://www.gnu.org/licenses/>.
# ##############################################################################
import asyncio
import datetime
import os
import platform
import re
import shutil
import socket
import sys
import threading
import traceback
import uuid
from collections import deque
from urllib.parse import uses_netloc
from urllib.request import FancyURLopener

from apscheduler.schedulers import SchedulerNotRunningError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dateutil import tz
from fake_useragent import UserAgent
from tornado.platform.asyncio import AnyThreadEventLoopPolicy

import sickrage
from sickrage.core.announcements import Announcements
from sickrage.core.api import API
from sickrage.core.auth import AuthServer
from sickrage.core.common import SD, SKIPPED, WANTED
from sickrage.core.config import Config
from sickrage.core.databases.cache import CacheDB
from sickrage.core.databases.main import MainDB
from sickrage.core.helpers import generate_secret, make_dir, get_lan_ip, restore_app_data, get_disk_space_usage, get_free_space, launch_browser, \
    torrent_webui_url, encryption, md5_file_hash
from sickrage.core.logger import Logger
from sickrage.core.nameparser.validator import check_force_season_folders
from sickrage.core.processors import auto_postprocessor
from sickrage.core.processors.auto_postprocessor import AutoPostProcessor
from sickrage.core.queues.postprocessor import PostProcessorQueue
from sickrage.core.queues.search import SearchQueue
from sickrage.core.queues.show import ShowQueue
from sickrage.core.searchers.backlog_searcher import BacklogSearcher
from sickrage.core.searchers.daily_searcher import DailySearcher
from sickrage.core.searchers.failed_snatch_searcher import FailedSnatchSearcher
from sickrage.core.searchers.proper_searcher import ProperSearcher
from sickrage.core.searchers.subtitle_searcher import SubtitleSearcher
from sickrage.core.searchers.trakt_searcher import TraktSearcher
from sickrage.core.tv.show import TVShow
from sickrage.core.tv.show.helpers import get_show_list
from sickrage.core.ui import Notifications
from sickrage.core.updaters.rsscache_updater import RSSCacheUpdater
from sickrage.core.updaters.show_updater import ShowUpdater
from sickrage.core.updaters.tz_updater import TimeZoneUpdater
from sickrage.core.upnp import UPNPClient
from sickrage.core.version_updater import VersionUpdater, SourceUpdateManager
from sickrage.core.webserver import WebServer
from sickrage.metadata import MetadataProviders
from sickrage.notifiers import NotifierProviders
from sickrage.providers import SearchProviders


class Core(object):
    def __init__(self):
        self.started = False
        self.loading_shows = False
        self.daemon = None
        self.pid = os.getpid()

        try:
            self.tz = tz.tzwinlocal() if tz.tzwinlocal else tz.tzlocal()
        except Exception:
            self.tz = tz.tzlocal()

        self.shows = {}
        self.shows_recent = deque(maxlen=5)

        self.private_key = None
        self.public_key = None

        self.main_db = None
        self.cache_db = None

        self.config_file = None
        self.data_dir = None
        self.cache_dir = None
        self.quiet = None
        self.no_launch = None
        self.disable_updates = None
        self.web_port = None
        self.web_host = None
        self.web_root = None
        self.developer = None
        self.db_type = None
        self.db_prefix = None
        self.db_host = None
        self.db_port = None
        self.db_username = None
        self.db_password = None
        self.debug = None
        self.newest_version_string = None

        self.naming_ep_type = ("%(seasonnumber)dx%(episodenumber)02d",
                               "s%(seasonnumber)02de%(episodenumber)02d",
                               "S%(seasonnumber)02dE%(episodenumber)02d",
                               "%(seasonnumber)02dx%(episodenumber)02d",
                               "S%(seasonnumber)02d E%(episodenumber)02d")
        self.sports_ep_type = ("%(seasonnumber)dx%(episodenumber)02d",
                               "s%(seasonnumber)02de%(episodenumber)02d",
                               "S%(seasonnumber)02dE%(episodenumber)02d",
                               "%(seasonnumber)02dx%(episodenumber)02d",
                               "S%(seasonnumber)02 dE%(episodenumber)02d")
        self.naming_ep_type_text = ("1x02", "s01e02", "S01E02", "01x02", "S01 E02",)
        self.naming_multi_ep_type = {0: ["-%(episodenumber)02d"] * len(self.naming_ep_type),
                                     1: [" - " + x for x in self.naming_ep_type],
                                     2: [x + "%(episodenumber)02d" for x in ("x", "e", "E", "x")]}
        self.naming_multi_ep_type_text = ("extend", "duplicate", "repeat")
        self.naming_sep_type = (" - ", " ")
        self.naming_sep_type_text = (" - ", "space")

        self.user_agent = 'SiCKRAGE.CE.1/({};{};{})'.format(platform.system(), platform.release(), str(uuid.uuid1()))
        self.languages = [language for language in os.listdir(sickrage.LOCALE_DIR) if '_' in language]
        self.client_web_urls = {'torrent': '', 'newznab': ''}

        self.notifier_providers = {}
        self.metadata_providers = {}
        self.search_providers = {}
        self.adba_connection = None
        self.log = None
        self.config = None
        self.alerts = None
        self.scheduler = None
        self.wserver = None
        self.google_auth = None
        self.show_queue = None
        self.search_queue = None
        self.postprocessor_queue = None
        self.version_updater = None
        self.show_updater = None
        self.tz_updater = None
        self.rsscache_updater = None
        self.daily_searcher = None
        self.failed_snatch_searcher = None
        self.backlog_searcher = None
        self.proper_searcher = None
        self.trakt_searcher = None
        self.subtitle_searcher = None
        self.auto_postprocessor = None
        self.upnp_client = None
        self.auth_server = None
        self.announcements = None
        self.api = None

    def start(self):
        self.started = True

        # thread name
        threading.currentThread().setName('CORE')

        # event loop policy that allows loop creation on any thread.
        asyncio.set_event_loop_policy(AnyThreadEventLoopPolicy())

        # scheduler
        self.scheduler = BackgroundScheduler({'apscheduler.timezone': 'UTC'})

        # init core classes
        self.api = API()
        self.main_db = MainDB(self.db_type, self.db_prefix, self.db_host, self.db_port, self.db_username, self.db_password)
        self.cache_db = CacheDB(self.db_type, self.db_prefix, self.db_host, self.db_port, self.db_username, self.db_password)
        self.notifier_providers = NotifierProviders()
        self.metadata_providers = MetadataProviders()
        self.search_providers = SearchProviders()
        self.log = Logger()
        self.config = Config()
        self.alerts = Notifications()
        self.wserver = WebServer()
        self.show_queue = ShowQueue()
        self.search_queue = SearchQueue()
        self.postprocessor_queue = PostProcessorQueue()
        self.version_updater = VersionUpdater()
        self.show_updater = ShowUpdater()
        self.tz_updater = TimeZoneUpdater()
        self.rsscache_updater = RSSCacheUpdater()
        self.daily_searcher = DailySearcher()
        self.failed_snatch_searcher = FailedSnatchSearcher()
        self.backlog_searcher = BacklogSearcher()
        self.proper_searcher = ProperSearcher()
        self.trakt_searcher = TraktSearcher()
        self.subtitle_searcher = SubtitleSearcher()
        self.auto_postprocessor = AutoPostProcessor()
        self.upnp_client = UPNPClient()
        self.announcements = Announcements()

        # authorization sso client
        self.auth_server = AuthServer()

        # check available space
        try:
            self.log.info("Performing disk space checks")
            total_space, available_space = get_free_space(self.data_dir)
            if available_space < 100:
                self.log.warning('Shutting down as SiCKRAGE needs some space to work. You\'ll get corrupted data otherwise. Only %sMB left', available_space)
                return
        except Exception:
            self.log.error('Failed getting disk space: %s', traceback.format_exc())

        # check if we need to perform a restore first
        if os.path.exists(os.path.abspath(os.path.join(self.data_dir, 'restore'))):
            self.log.info('Performing restore of backup files')
            success = restore_app_data(os.path.abspath(os.path.join(self.data_dir, 'restore')), self.data_dir)
            self.log.info("Restoring SiCKRAGE backup: %s!" % ("FAILED", "SUCCESSFUL")[success])
            if success:
                # self.main_db = MainDB(self.db_type, self.db_prefix, self.db_host, self.db_port, self.db_username, self.db_password)
                # self.cache_db = CacheDB(self.db_type, self.db_prefix, self.db_host, self.db_port, self.db_username, self.db_password)
                shutil.rmtree(os.path.abspath(os.path.join(self.data_dir, 'restore')), ignore_errors=True)

        # migrate old database file names to new ones
        if os.path.isfile(os.path.abspath(os.path.join(self.data_dir, 'sickbeard.db'))):
            if os.path.isfile(os.path.join(self.data_dir, 'sickrage.db')):
                helpers.move_file(os.path.join(self.data_dir, 'sickrage.db'),
                                  os.path.join(self.data_dir, '{}.bak-{}'
                                               .format('sickrage.db',
                                                       datetime.datetime.now().strftime(
                                                           '%Y%m%d_%H%M%S'))))

            helpers.move_file(os.path.abspath(os.path.join(self.data_dir, 'sickbeard.db')),
                              os.path.abspath(os.path.join(self.data_dir, 'sickrage.db')))

        # init encryption public and private keys
        encryption.initialize()

        # load config
        self.config.load()

        # set language
        self.config.change_gui_lang(self.config.gui_lang)

        # set socket timeout
        socket.setdefaulttimeout(self.config.socket_timeout)

        # setup logger settings
        self.log.logSize = self.config.log_size
        self.log.logNr = self.config.log_nr
        self.log.logFile = os.path.join(self.data_dir, 'logs', 'sickrage.log')
        self.log.debugLogging = self.config.debug
        self.log.consoleLogging = not self.quiet

        # start logger
        self.log.start()

        # perform database startup actions
        for db in [self.main_db, self.cache_db]:
            # perform integrity check
            self.log.info("Performing integrity check on {} database".format(db.name))
            db.integrity_check()

            # migrate database
            self.log.info("Performing migrations on {} database".format(db.name))
            db.migrate()

            # upgrade database
            self.log.info("Performing upgrades on {} database".format(db.name))
            db.upgrade()

            # cleanup
            self.log.info("Performing cleanup on {} database".format(db.name))
            db.cleanup()

        # user agent
        if self.config.random_user_agent:
            self.user_agent = UserAgent().random

        uses_netloc.append('scgi')
        FancyURLopener.version = self.user_agent

        # set torrent client web url
        torrent_webui_url(True)

        if self.config.default_page not in ('schedule', 'history', 'IRC'):
            self.config.default_page = 'home'

        # attempt to help prevent users from breaking links by using a bad url
        if not self.config.anon_redirect.endswith('?'):
            self.config.anon_redirect = ''

        if not re.match(r'\d+\|[^|]+(?:\|[^|]+)*', self.config.root_dirs):
            self.config.root_dirs = ''

        self.config.naming_force_folders = check_force_season_folders()

        if self.config.nzb_method not in ('blackhole', 'sabnzbd', 'nzbget'):
            self.config.nzb_method = 'blackhole'

        if self.config.torrent_method not in ('blackhole', 'utorrent', 'transmission', 'deluge', 'deluged',
                                              'download_station', 'rtorrent', 'qbittorrent', 'mlnet', 'putio'):
            self.config.torrent_method = 'blackhole'

        if self.config.autopostprocessor_freq < self.config.min_autopostprocessor_freq:
            self.config.autopostprocessor_freq = self.config.min_autopostprocessor_freq

        if self.config.daily_searcher_freq < self.config.min_daily_searcher_freq:
            self.config.daily_searcher_freq = self.config.min_daily_searcher_freq

        if self.config.backlog_searcher_freq < self.config.min_backlog_searcher_freq:
            self.config.backlog_searcher_freq = self.config.min_backlog_searcher_freq

        if self.config.version_updater_freq < self.config.min_version_updater_freq:
            self.config.version_updater_freq = self.config.min_version_updater_freq

        if self.config.subtitle_searcher_freq < self.config.min_subtitle_searcher_freq:
            self.config.subtitle_searcher_freq = self.config.min_subtitle_searcher_freq

        if self.config.failed_snatch_age < self.config.min_failed_snatch_age:
            self.config.failed_snatch_age = self.config.min_failed_snatch_age

        if self.config.proper_searcher_interval not in ('15m', '45m', '90m', '4h', 'daily'):
            self.config.proper_searcher_interval = 'daily'

        if self.config.showupdate_hour < 0 or self.config.showupdate_hour > 23:
            self.config.showupdate_hour = 0

        # add version checker job
        self.scheduler.add_job(
            self.version_updater.task,
            IntervalTrigger(
                hours=self.config.version_updater_freq,
                timezone='utc'
            ),
            name=self.version_updater.name,
            id=self.version_updater.name
        )

        # add network timezones updater job
        self.scheduler.add_job(
            self.tz_updater.task,
            IntervalTrigger(
                days=1,
                timezone='utc'
            ),
            name=self.tz_updater.name,
            id=self.tz_updater.name
        )

        # add show updater job
        self.scheduler.add_job(
            self.show_updater.task,
            IntervalTrigger(
                days=1,
                start_date=datetime.datetime.now().replace(hour=self.config.showupdate_hour),
                timezone='utc'
            ),
            name=self.show_updater.name,
            id=self.show_updater.name
        )

        # add rss cache updater job
        self.scheduler.add_job(
            self.rsscache_updater.task,
            IntervalTrigger(
                minutes=15,
                timezone='utc'
            ),
            name=self.rsscache_updater.name,
            id=self.rsscache_updater.name
        )

        # add daily search job
        self.scheduler.add_job(
            self.daily_searcher.task,
            IntervalTrigger(
                minutes=self.config.daily_searcher_freq,
                start_date=datetime.datetime.now() + datetime.timedelta(minutes=4),
                timezone='utc'
            ),
            name=self.daily_searcher.name,
            id=self.daily_searcher.name
        )

        # add failed snatch search job
        self.scheduler.add_job(
            self.failed_snatch_searcher.task,
            IntervalTrigger(
                hours=1,
                start_date=datetime.datetime.now() + datetime.timedelta(minutes=4),
                timezone='utc'
            ),
            name=self.failed_snatch_searcher.name,
            id=self.failed_snatch_searcher.name
        )

        # add backlog search job
        self.scheduler.add_job(
            self.backlog_searcher.task,
            IntervalTrigger(
                minutes=self.config.backlog_searcher_freq,
                start_date=datetime.datetime.now() + datetime.timedelta(minutes=30),
                timezone='utc'
            ),
            name=self.backlog_searcher.name,
            id=self.backlog_searcher.name
        )

        # add auto-postprocessing job
        self.scheduler.add_job(
            self.auto_postprocessor.task,
            IntervalTrigger(
                minutes=self.config.autopostprocessor_freq,
                timezone='utc'
            ),
            name=self.auto_postprocessor.name,
            id=self.auto_postprocessor.name
        )

        # add find proper job
        self.scheduler.add_job(
            self.proper_searcher.task,
            IntervalTrigger(
                minutes={
                    '15m': 15,
                    '45m': 45,
                    '90m': 90,
                    '4h': 4 * 60,
                    'daily': 24 * 60
                }[self.config.proper_searcher_interval],
                timezone='utc'
            ),
            name=self.proper_searcher.name,
            id=self.proper_searcher.name
        )

        # add trakt.tv checker job
        self.scheduler.add_job(
            self.trakt_searcher.task,
            IntervalTrigger(
                hours=1,
                timezone='utc'
            ),
            name=self.trakt_searcher.name,
            id=self.trakt_searcher.name
        )

        # add subtitles finder job
        self.scheduler.add_job(
            self.subtitle_searcher.task,
            IntervalTrigger(
                hours=self.config.subtitle_searcher_freq,
                timezone='utc'
            ),
            name=self.subtitle_searcher.name,
            id=self.subtitle_searcher.name
        )

        # add upnp client job
        self.scheduler.add_job(
            self.upnp_client.task,
            IntervalTrigger(
                seconds=self.upnp_client._nat_portmap_lifetime,
                timezone='utc'
            ),
            name=self.upnp_client.name,
            id=self.upnp_client.name
        )

        # add announcements job
        self.scheduler.add_job(
            self.announcements.task,
            IntervalTrigger(
                minutes=15,
                timezone='utc'
            ),
            name=self.announcements.name,
            id=self.announcements.name
        )

        # add provider URL update job
        self.scheduler.add_job(
            self.search_providers.task,
            IntervalTrigger(
                hours=1,
                timezone='utc'
            ),
            name=self.search_providers.name,
            id=self.search_providers.name
        )

        # start queues
        self.search_queue.start_worker(self.config.max_queue_workers)
        self.show_queue.start_worker(self.config.max_queue_workers)
        self.postprocessor_queue.start_worker(self.config.max_queue_workers)

        # start web server
        self.wserver.start()

        # fire off jobs now
        self.scheduler.get_job(self.version_updater.name).modify(next_run_time=datetime.datetime.utcnow())
        self.scheduler.get_job(self.tz_updater.name).modify(next_run_time=datetime.datetime.utcnow())
        self.scheduler.get_job(self.announcements.name).modify(next_run_time=datetime.datetime.utcnow())
        self.scheduler.get_job(self.search_providers.name).modify(next_run_time=datetime.datetime.utcnow())

        # start scheduler service
        self.scheduler.start()

        # load shows
        self.scheduler.add_job(self.load_shows)

        # launch browser window
        if all([not sickrage.app.no_launch, sickrage.app.config.launch_browser]):
            self.scheduler.add_job(launch_browser, args=[('http', 'https')[sickrage.app.config.enable_https],
                                                         sickrage.app.config.web_host, sickrage.app.config.web_port])

        self.log.info("SiCKRAGE :: STARTED")
        self.log.info("SiCKRAGE :: APP VERSION:[{}]".format(sickrage.version()))
        self.log.info("SiCKRAGE :: CONFIG VERSION:[v{}]".format(self.config.config_version))
        self.log.info("SiCKRAGE :: DATABASE VERSION:[v{}]".format(self.main_db.version))
        self.log.info("SiCKRAGE :: DATABASE TYPE:[{}]".format(self.db_type))
        self.log.info("SiCKRAGE :: URL:[{}://{}:{}/{}]".format(('http', 'https')[self.config.enable_https],
                                                               (self.config.web_host, get_lan_ip())[self.config.web_host == '0.0.0.0'],
                                                               self.config.web_port,
                                                               self.config.web_root))

    def load_shows(self):
        threading.currentThread().setName('CORE')

        session = self.main_db.session()

        self.log.info('Loading initial shows list')

        self.loading_shows = True

        self.shows = {}
        for query in session.query(MainDB.TVShow).with_entities(MainDB.TVShow.indexer_id, MainDB.TVShow.indexer, MainDB.TVShow.name):
            try:
                self.log.info('Loading show {}'.format(query.name))
                self.shows.update({(query.indexer_id, query.indexer): TVShow(query.indexer_id, query.indexer)})
            except Exception as e:
                self.log.debug('There was an error loading show: {}'.format(query.name))

        self.loading_shows = False

        self.log.info('Loading initial shows list finished')

    def shutdown(self, restart=False):
        if self.started:
            self.log.info('SiCKRAGE IS {}!!!'.format(('SHUTTING DOWN', 'RESTARTING')[restart]))

            # shutdown scheduler
            if self.scheduler:
                try:
                    self.scheduler.shutdown()
                except (SchedulerNotRunningError, RuntimeError):
                    pass

            # shutdown webserver
            if self.wserver:
                self.wserver.shutdown()

            # stop queues
            self.search_queue.shutdown()
            self.show_queue.shutdown()
            self.postprocessor_queue.shutdown()

            # log out of ADBA
            if self.adba_connection:
                self.log.debug("Shutting down ANIDB connection")
                self.adba_connection.stop()

            # save shows
            self.log.info('Saving all shows to the database')
            for show in self.shows.values():
                show.save()

            # save settings
            self.config.save()

            # shutdown logging
            if self.log:
                self.log.close()

        if restart:
            os.execl(sys.executable, sys.executable, *sys.argv)

        if sickrage.app.daemon:
            sickrage.app.daemon.stop()

        self.started = False
