#!/usr/bin/env python

# usage: pipenv shell && mitmdump -q -s dbproxy.py '!~a' # Attention dump, not proxy
#
# remember to add certificates in your browser/os for TLS/HTTPS
# certs docs: https://docs.mitmproxy.org/stable/concepts-certificates/
# filter expressions docs: https://docs.mitmproxy.org/stable/concepts-filters/

import mimetypes
import os, time
import yaml
from pathlib import Path

from mitmproxy import ctx
from mitmproxy import flowfilter
from mitmproxy import http

# mitmproxy http.HTTPFlow 'headers' type is multidict
def _get( multidict, key ):
    try:
        return multidict[ key ]
    except KeyError:
        return ''


# yaml helping records
records = {}
recs = [ 'ignored-urls', 'tagged-urls', 'recorded' ]

def load_records( conf ):
    global recs
    global records
    for r in recs:
        with open( conf.get( r ), 'r' ) as f:
            records[ r ] = yaml.load( f, Loader=yaml.SafeLoader )
        if not records[ r ]:
            records[ r ] = []
        f.close()
    # print( records )


time_last_req = 0.0

# https://ieeexplore.ieee.org/document/7796839
def identify( request, conf ):
    global time_last_req
    __time_window_next = 2.0
    __reset_timer = 4.0

    if request.timestamp_start - time_last_req > __reset_timer or time_last_req == 0:
        time_last_req = request.timestamp_start
        return True
    ua = True 

    # TODO make this into regex
    if request.url in conf.get( 'ignored-urls' ):
        ua = False
    if request.timestamp_start - time_last_req < __time_window_next:
        ua = False
    return ua


def archive( request, conf ):
    global records
    # conf.get( 'tagged-urls' ))

    if conf.get( 'mode' ) == 'archive':
        subprocess.Popen( [ 
	      'archivebox', 'add', request.url ], 
	      stdout=subprocess.DEVNULL, 
		  stderr=subprocess.DEVNULL, 
	      stdin=subprocess.DEVNULL,
          cdw=conf.get( 'archivebox-path' ))

    elif conf.get( 'mode' ) == 'record':
        records.get( 'recorded' )[ request.url ] = True
        # print( records.get( 'recorded' ))
        with open( conf.get( 'recorded' ), 'w' ) as f:
            yaml.dump( records[ 'recorded' ], f )
        f.close()



_config_path = 'config-archive-proxy.yaml'

class ArchiveBoxProxy:
    def __init__( self ) -> None:
        with open( _config_path, 'r' ) as f:
            self.config = yaml.load( f, Loader=yaml.SafeLoader )
        print( self.config )

        load_records( self.config )

    def request(self, flow: http.HTTPFlow) -> None:
        # // in requests - identify user action
        ua = identify( flow.request, self.config )

        if ua:
            time_created = time.strftime( "%Y-%M-%d %H:%M:%S", time.localtime( flow.timestamp_created))
            print( f'''{ flow.request.url }''' )

            archive( flow.request, self.config )


addons = [ArchiveBoxProxy()]
