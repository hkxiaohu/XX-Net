#!/usr/bin/env python
# coding:utf-8

import sys
import os
import errno
import binascii
import time
import collections
import random
import thread
import socket
import select
import Queue
import httplib
import urlparse
import struct
import traceback
import logging
import threading
import operator

current_path = os.path.dirname(os.path.abspath(__file__))
python_path = os.path.abspath( os.path.join(current_path, os.pardir, os.pardir, os.pardir, 'python27', '1.0'))
if sys.platform == "win32":
    win32_lib = os.path.abspath( os.path.join(python_path, 'lib', 'win32'))
    sys.path.append(win32_lib)
elif sys.platform == "linux" or sys.platform == "linux2":
    win32_lib = os.path.abspath( os.path.join(python_path, 'lib', 'linux'))
    sys.path.append(win32_lib)

import OpenSSL
SSLError = OpenSSL.SSL.WantReadError

from google_ip import google_ip

from openssl_wrap import SSLConnection

NetWorkIOError = (socket.error, SSLError, OpenSSL.SSL.Error, OSError)

g_cacertfile = os.path.join(current_path, "cacert.pem")


class Connect_pool():
    pool_lock = threading.Lock()
    not_empty = threading.Condition(pool_lock)
    pool = {}

    def qsize(self):
        return len(self.pool)

    def put(self, item):
        handshake_time, sock = item
        self.not_empty.acquire()
        try:
            self.pool[sock] = handshake_time
            self.not_empty.notify()
        finally:
            self.not_empty.release()

    def get(self, block=True, timeout=None):
        self.not_empty.acquire()
        try:
            if not block:
                if not self.qsize():
                    return None
            elif timeout is None:
                while not self.qsize():
                    self.not_empty.wait()
            elif timeout < 0:
                raise ValueError("'timeout' must be a positive number")
            else:
                end_time = time.time() + timeout
                while not self.qsize():
                    remaining = end_time - time.time()
                    if remaining <= 0.0:
                        return None
                    self.not_empty.wait(remaining)

            item = self._get()
            return item
        finally:
            self.not_empty.release()

    def get_nowait(self):
        return self.get(block=False)

    def _get(self):
        #pool = sorted(self.pool.items(), key=operator.itemgetter(1))
        #k,v = pool[0]
        #self.pool.pop(k)
        #return (v, k)

        fastest_time = 9999
        fastest_sock = None
        for sock in self.pool:
            time = self.pool[sock]
            if time < fastest_time:
                fastest_time = time
                fastest_sock = sock

        self.pool.pop(fastest_sock)
        return (fastest_time, fastest_sock)

    def get_slowest(self):
        self.not_empty.acquire()
        try:
            if not self.qsize():
                raise ValueError("no item")

            slowest_time = 0
            slowest_sock = None
            for sock in self.pool:
                time = self.pool[sock]
                if time > slowest_time:
                    slowest_time = time
                    slowest_sock = sock

            self.pool.pop(slowest_sock)
            return (slowest_time, slowest_sock)
        finally:
            self.not_empty.release()

    def to_string(self):
        str = ''
        self.pool_lock.acquire()
        try:
            pool = sorted(self.pool.items(), key=operator.itemgetter(1))
            i = 0
            for item in pool:
                sock,t = item
                str += "%d \t %s handshake:%d create:%d\r\n" % (i, sock.ip, t, time.time() -sock.last_use_time)
                i += 1
        finally:
            self.pool_lock.release()

        return str


class Https_connection_manager(object):

    thread_num_lock = threading.Lock()
    thread_num = 0

    protocol_version = 'HTTP/1.1'
    skip_headers = frozenset(['Vary',
                              'Via',
                              'X-Forwarded-For',
                              'Proxy-Authorization',
                              'Proxy-Connection',
                              'Upgrade',
                              'X-Chrome-Variations',
                              'Connection',
                              'Cache-Control'])

    ssl_ciphers = ['ECDHE-ECDSA-AES256-SHA',
                            'ECDHE-RSA-AES256-SHA',
                            'DHE-RSA-CAMELLIA256-SHA',
                            'DHE-DSS-CAMELLIA256-SHA',
                            'DHE-RSA-AES256-SHA',
                            'DHE-DSS-AES256-SHA',
                            'ECDH-RSA-AES256-SHA',
                            'ECDH-ECDSA-AES256-SHA',
                            'CAMELLIA256-SHA',
                            'AES256-SHA',
                            'ECDHE-ECDSA-RC4-SHA',
                            'ECDHE-ECDSA-AES128-SHA',
                            'ECDHE-RSA-RC4-SHA',
                            'ECDHE-RSA-AES128-SHA',
                            'DHE-RSA-CAMELLIA128-SHA',
                            'DHE-DSS-CAMELLIA128-SHA',
                            'DHE-RSA-AES128-SHA',
                            'DHE-DSS-AES128-SHA',
                            'ECDH-RSA-RC4-SHA',
                            'ECDH-RSA-AES128-SHA',
                            'ECDH-ECDSA-RC4-SHA',
                            'ECDH-ECDSA-AES128-SHA',
                            'SEED-SHA',
                            'CAMELLIA128-SHA',
                            'RC4-SHA',
                            'RC4-MD5',
                            'AES128-SHA',
                            'ECDHE-ECDSA-DES-CBC3-SHA',
                            'ECDHE-RSA-DES-CBC3-SHA',
                            'EDH-RSA-DES-CBC3-SHA',
                            'EDH-DSS-DES-CBC3-SHA',
                            'ECDH-RSA-DES-CBC3-SHA',
                            'ECDH-ECDSA-DES-CBC3-SHA',
                            'DES-CBC3-SHA',
                            'TLS_EMPTY_RENEGOTIATION_INFO_SCSV']

    def __init__(self):
        # http://docs.python.org/dev/library/ssl.html
        # http://blog.ivanristic.com/2009/07/examples-of-the-information-collected-from-ssl-handshakes.html
        # http://src.chromium.org/svn/trunk/src/net/third_party/nss/ssl/sslenum.c
        # openssl s_server -accept 443 -key CA.crt -cert CA.crt

        self.max_retry = 3
        self.timeout = 3
        self.max_timeout = 5
        self.max_thread_num = 40
        self.connection_pool_num = 30

        self.conn_pool = Connect_pool() #Queue.PriorityQueue()


        # set_ciphers as Modern Browsers
        # http://www.openssl.org/docs/apps/ciphers.html
        #ssl_ciphers = [x for x in self.ssl_ciphers if random.random() > 0.5]

        self.openssl_context = SSLConnection.context_builder(ssl_version="TLSv1") #, ca_certs=g_cacertfile) #, cipher_suites=ssl_ciphers)

        # ref: http://vincent.bernat.im/en/blog/2011-ssl-session-reuse-rfc5077.html
        self.openssl_context.set_session_id(binascii.b2a_hex(os.urandom(10)))
        if hasattr(OpenSSL.SSL, 'SESS_CACHE_BOTH'):
            self.openssl_context.set_session_cache_mode(OpenSSL.SSL.SESS_CACHE_BOTH)

    def save_ssl_connection_for_reuse(self, ssl_sock):
        ssl_sock.last_use_time = time.time()
        self.conn_pool.put( (ssl_sock.handshake_time, ssl_sock) )

        while self.conn_pool.qsize() > self.connection_pool_num:
            t, ssl_sock = self.conn_pool.get_slowest()
            if t < 300:
                #self.conn_pool.put( (ssl_sock.handshake_time, ssl_sock) )
                return
            ssl_sock.close()

    def create_ssl_connection(self):

        def _create_ssl_connection(ip_port):
            sock = None
            ssl_sock = None
            ip = ip_port[0]
            try:
                sock = socket.socket(socket.AF_INET if ':' not in ip_port[0] else socket.AF_INET6)
                # set reuseaddr option to avoid 10048 socket error
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                # set struct linger{l_onoff=1,l_linger=0} to avoid 10048 socket error
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                # resize socket recv buffer 8K->32K to improve browser releated application performance
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32*1024)
                # disable negal algorithm to send http request quickly.
                sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, True)
                # set a short timeout to trigger timeout retry more quickly.

                sock.settimeout(self.timeout)


                ssl_sock = SSLConnection(self.openssl_context, sock)
                ssl_sock.set_connect_state()

                # pick up the certificate
                #server_hostname = random_hostname() if (cache_key or '').startswith('google_') or hostname.endswith('.appspot.com') else None
                #if server_hostname and hasattr(ssl_sock, 'set_tlsext_host_name'):
                #    ssl_sock.set_tlsext_host_name(server_hostname)

                connect_time = 0
                handshake_time = 0
                time_begin = time.time()
                ssl_sock.connect(ip_port)
                time_connected = time.time()
                ssl_sock.do_handshake()
                time_handshaked = time.time()

                connect_time = int((time_connected - time_begin) * 1000)
                handshake_time = int((time_handshaked - time_connected) * 1000)

                google_ip.update_ip(ip, handshake_time)
                logging.debug("create_ssl update ip:%s time:%d", ip, handshake_time)
                # sometimes, we want to use raw tcp socket directly(select/epoll), so setattr it to ssl socket.
                ssl_sock.ip = ip
                ssl_sock.sock = sock
                ssl_sock.create_time = time_begin
                ssl_sock.handshake_time = handshake_time


                def verify_SSL_certificate_issuer(ssl_sock):
                    cert = ssl_sock.get_peer_certificate()
                    if not cert:
                        raise socket.error(' certficate is none')

                    issuer_commonname = next((v for k, v in cert.get_issuer().get_components() if k == 'CN'), '')
                    if not issuer_commonname.startswith('Google'):
                        raise socket.error(' certficate is issued by %r, not Google' % ( issuer_commonname))

                #verify_SSL_certificate_issuer(ssl_sock)

                return ssl_sock
            except Exception as e:
                logging.debug("create_ssl %s fail:%s c:%d h:%d", ip, e, connect_time, handshake_time)
                google_ip.report_connect_fail(ip)


                if ssl_sock:
                    ssl_sock.close()
                if sock:
                    sock.close()
                return False


        def connect_thread():
            try:
                while self.conn_pool.qsize() < self.connection_pool_num:
                    ip_str = google_ip.get_gws_ip()
                    if not ip_str:
                        logging.warning("no gws ip")
                        break

                    port = 443
                    #logging.debug("create ssl conn %s", ip_str)
                    ssl_sock = _create_ssl_connection( (ip_str, port) )
                    if ssl_sock:
                        ssl_sock.last_use_time = time.time()
                        self.conn_pool.put((ssl_sock.handshake_time, ssl_sock))
            finally:
                self.thread_num_lock.acquire()
                self.thread_num -= 1
                self.thread_num_lock.release()

        def create_more_connection():
            target_thread_num = min(self.max_thread_num, (self.connection_pool_num - self.conn_pool.qsize()))
            while self.thread_num < target_thread_num:
                self.thread_num_lock.acquire()
                self.thread_num += 1
                self.thread_num_lock.release()
                p = threading.Thread(target = connect_thread)
                p.daemon = True
                p.start()


        while True:
            ret = self.conn_pool.get_nowait()
            if ret:
                handshake_time, ssl_sock = ret
            else:
                ssl_sock = None
                break

            if time.time() - ssl_sock.last_use_time < 210: # gws ssl connection can keep for 230s after created
                logging.debug("ssl_pool.get:%s handshake:%d", ssl_sock.ip, handshake_time)
                break
            else:
                ssl_sock.close()
                continue

        conn_num = self.conn_pool.qsize()
        logging.debug("ssl conn_num:%d", conn_num)
        if conn_num < self.connection_pool_num:
            create_more_connection()

        if ssl_sock:
            return ssl_sock
        else:
            ret = self.conn_pool.get(True, self.max_timeout)
            if ret:
                handshake_time, ssl_sock = ret
                return ssl_sock
            else:
                logging.debug("create ssl timeout fail.")
                return None




    def _request(self, sock, method, path, protocol_version, headers, payload, bufsize=8192):
        skip_headers = self.skip_headers

        request_data = '%s %s %s\r\n' % (method, path, protocol_version)
        request_data += ''.join('%s: %s\r\n' % (k, v) for k, v in headers.items() if k not in skip_headers)
        request_data += '\r\n'

        if isinstance(payload, bytes):
            sock.sendall(request_data.encode() + payload)
        elif hasattr(payload, 'read'):
            sock.sendall(request_data)
            while 1:
                data = payload.read(bufsize)
                if not data:
                    break
                sock.sendall(data)
        else:
            raise TypeError('_request(payload) must be a string or buffer, not %r' % type(payload))

        response = httplib.HTTPResponse(sock, buffering=True)
        try:
            response.begin()
        except httplib.BadStatusLine:
            response = None
        return response

    # Caller:  gae_urlfetch
    # so scheme always be https
    def request(self, method, url, payload=None, headers={}):
        scheme, netloc, path, _, query, _ = urlparse.urlparse(url)
        if netloc.rfind(':') <= netloc.rfind(']'):
            host = netloc
        else:
            host, _, port = netloc.rpartition(':')
            path += '?' + query

        if 'Host' not in headers:
            headers['Host'] = host

        for i in range(self.max_retry):
            ssl_sock = None
            try:
                ssl_sock = self.create_ssl_connection()
                if not ssl_sock:
                    logging.debug('request "%s %s" create_ssl_connection fail', method, url)
                    continue

                response = self._request(ssl_sock, method, path, self.protocol_version, headers, payload)
                if response:
                    response.ssl_sock = ssl_sock
                return response

            except Exception as e:
                logging.debug('request "%s %s" failed:%s', method, url, e)
                if ssl_sock:
                    ssl_sock.close()
                if i == self.max_retry - 1:
                    raise
                else:
                    continue


class Forward_connection_manager():
    max_retry = 3
    max_timeout = 3
    tcp_connection_cache = Queue.PriorityQueue()

    def create_connection(self, sock_life=5):
        def _create_connection(ip_port, queobj, delay=0):
            if delay != 0:
                time.sleep(delay)
            ip = ip_port[0]
            sock = None
            try:
                # create a ipv4/ipv6 socket object
                sock = socket.socket(socket.AF_INET if ':' not in ip else socket.AF_INET6)
                # set reuseaddr option to avoid 10048 socket error
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                # resize socket recv buffer 8K->32K to improve browser releated application performance
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32*1024)
                # disable negal algorithm to send http request quickly.
                sock.setsockopt(socket.SOL_TCP, socket.TCP_NODELAY, True)
                # set a short timeout to trigger timeout retry more quickly.
                sock.settimeout(self.max_timeout)
                # start connection time record
                start_time = time.time()

                conn_time = 0
                # TCP connect
                sock.connect(ip_port)

                # record TCP connection time
                conn_time = time.time() - start_time
                google_ip.update_ip(ip, conn_time * 2000)
                #logging.info("create_tcp update ip:%s time:%d", ip, conn_time * 2000)
                logging.debug("tcp conn %s time:%d", ip, conn_time * 1000)

                # put ssl socket object to output queobj
                queobj.put(sock)
            except (socket.error, OSError) as e:
                # any socket.error, put Excpetions to output queobj.
                queobj.put(e)
                conn_time = int((time.time() - start_time) * 1000)
                logging.debug("tcp conn %s fail t:%d", ip, conn_time)
                google_ip.report_connect_fail(ip)
                #logging.info("create_tcp report fail ip:%s", ip)
                if sock:
                    sock.close()

        def recycle_connection(count, queobj):
            for i in range(count):
                sock = queobj.get()
                if sock and not isinstance(sock, Exception):
                    self.tcp_connection_cache.put((time.time(), sock))

        try:
            ctime, sock = self.tcp_connection_cache.get_nowait()
            if time.time() - ctime < sock_life:
                return sock
        except Queue.Empty:
            pass


        port = 443
        for j in range(self.max_retry):
            addresses = []
            for i in range(3):
                ip = google_ip.get_gws_ip()
                if not ip:
                    logging.warning("no gws ip.")
                    return
                addresses.append((ip, port))

            addrs = addresses
            queobj = Queue.Queue()
            delay = 0
            for addr in addrs:
                thread.start_new_thread(_create_connection, (addr, queobj, delay))
                #delay += 0.05
            for i in range(len(addrs)):
                result = queobj.get()
                if not isinstance(result, (socket.error, OSError)):
                    thread.start_new_thread(recycle_connection, (len(addrs)-i-1, queobj))
                    return result
        logging.warning('create_connection fail.')


    def forward_socket(self, local, remote, timeout=60, tick=2, bufsize=8192):
        try:
            timecount = timeout
            while 1:
                timecount -= tick
                if timecount <= 0:
                    break
                (ins, _, errors) = select.select([local, remote], [], [local, remote], tick)
                if errors:
                    break
                if not ins:
                    continue

                for sock in ins:
                    data = sock.recv(bufsize)
                    if not data:
                        return

                    if sock is remote:
                        local.sendall(data)
                        timecount = timeout
                    else:
                        remote.sendall(data)
                        timecount = timeout
        except NetWorkIOError as e:
            if e.args[0] not in (errno.ECONNABORTED, errno.ECONNRESET, errno.ENOTCONN, errno.EPIPE):
                raise
        finally:
            if local:
                local.close()
            if remote:
                remote.close()
            logging.debug("forward closed.")



https_manager = Https_connection_manager()
forwork_manager = Forward_connection_manager()


def test_pool():
    pool = Connect_pool()
    pool.put((3, "c"))
    pool.put((1, "a"))
    pool.put((2, "b"))

    t, s = pool.get()
    print s

    t, s = pool.get()
    print s

    t, s = pool.get()
    print s


def test_pool_speed():
    pool = Connect_pool()
    for i in range(100):
        pool.put((i, "%d"%i))

    start = time.time()
    t, s = pool.get()
    print time.time() - start
    print s
    # sort time is 5ms for 10000
    # sort time is 0ms for 100

if __name__ == "__main__":
    test_pool_speed()