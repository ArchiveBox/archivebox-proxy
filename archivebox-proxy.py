#!/usr/bin/env python
# usage: mitmdump -s archive-proxy.py
#
# remember to add your own mitmproxy authorative certs in your browser/os!
# certs docs: https://docs.mitmproxy.org/stable/concepts-certificates/
# filter expressions docs: https://docs.mitmproxy.org/stable/concepts-filters/
import logging
import mimetypes
import os
from pathlib import Path

from mitmproxy import ctx
from mitmproxy import flowfilter
from mitmproxy import http


class ArchiveBoxProxy:
    def load(self, loader):
#        self.filter = ctx.options.dumper_filter

        loader.add_option(
            name="dumper_folder",
            typespec=str,
            default="httpdump",
            help="content dump destination folder",
        )

#    def configure(self, updated):
#        if "dumper_filter" in updated:
#            self.filter = ctx.options.dumper_filter

#    def response(self, flow: http.HTTPFlow) -> None:
#        if flowfilter.match(self.filter, flow):
		 # self.dump(flow)

    def request(self, flow: http.HTTPFlow) -> None:
		# logging.info( 'requesti-log' )
		# print ( 'request-print' )
        try:
			# print( 'referer: ' + str(flow.request.headers[ 'referer' ]))
            x = flow.request.headers[ 'referer' ]
        except KeyError:
			# print ( 'no referer' )
            print( 'headers: ' + str( flow.request.headers ))
            print( 'request: ' + str( flow.request ))
            print( 'flow: ' + str( flow ))
            print( 'url: ' + str( flow.request.url ))
		#if flow.request.headers[ 'referer' ]:
		#    print( 'request: ' + str(flow.request ))
		#    print( 'host: ' + str(flow.request.host ))
		#    print( 'pretty_host: ' + str(flow.request.pretty_host ))
		#    print( 'host_header: ' + str(flow.request.host_header ))
		#    print( 'print: ' + str( flow ))
		#    print( 'headers: ' + str(flow.request.headers))
		# print( 'referer: ' + str(flow.request.headers[ 'referer' ]))
        # url = ''
        # subprocess.Popen(['archivebox', 'add', url, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


#    def request_header(self, flow: http.HTTPFlow) -> None:
#        print ( 'request_header' )

#    def dump(self, flow: http.HTTPFlow):
#        print( 'dump' )
#        if not flow.response:
#            return

addons = [ArchiveBoxProxy()]
