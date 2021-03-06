# -*- coding: utf-8 -*-
# Copyright (c) 2011 Plivo Team. See LICENSE for details.

"""
Event Socket class
"""

import types
import gevent
import gevent.socket as socket
import gevent.queue as queue
import gevent.pool
from gevent import GreenletExit
from gevent.coros import RLock
from plivo.core.freeswitch.commands import Commands
from plivo.core.freeswitch.eventtypes import Event, CommandResponse, ApiResponse, BgapiResponse, JsonEvent
from plivo.core.errors import LimitExceededError, ConnectError


EOL = "\n"
MAXLINES_PER_EVENT = 1000



class EventSocket(Commands):
    '''EventSocket class'''
    def __init__(self, filter="ALL", pool_size=5000, eventjson=True):
        self._is_eventjson = eventjson
        # Callbacks for reading events and sending responses.
        self._response_callbacks = {'api/response':self._api_response,
                                    'command/reply':self._command_reply,
                                    'auth/request':self._auth_request,
                                    'text/disconnect-notice':self._disconnect_notice
                                   }
        if self._is_eventjson:
            self._response_callbacks['text/event-json'] = self._event_json
        else:
            self._response_callbacks['text/event-plain'] = self._event_plain
        # Closing state flag
        self._closing_state = False
        # Default event filter.
        self._filter = filter
        # Synchronized Gevent based Queue for response events.
        self._response_queue = queue.Queue(1)
        # Lock to force eventsocket commands to be sequential.
        self._lock = RLock()
        # Sets connected to False.
        self.connected = False
        self._running = True
        # Creates pool for spawning if pool_size > 0
        if pool_size > 0:
            self.pool = gevent.pool.Pool(pool_size)
        else:
            self.pool = None
        # Handler thread
        self._handler_thread = None

    def _spawn(self, func, *args, **kwargs):
        '''
        Spawns with or without pool.
        '''
        if self.pool:
            self.pool.spawn(func, *args, **kwargs)
        else:
            gevent.spawn_raw(func, *args, **kwargs)

    def is_connected(self):
        '''
        Checks if connected and authenticated to eventsocket.

        Returns True or False.
        '''
        return self.connected

    def start_event_handler(self):
        '''
        Starts Event handler in background.
        '''
        self._handler_thread = gevent.spawn(self.handle_events)

    def stop_event_handler(self):
        '''
        Stops Event handler.
        '''
        if self._handler_thread:
            self._handler_thread.kill()

    def handle_events(self):
        '''
        Gets and Dispatches events in an endless loop using gevent spawn.
        '''
        while True:
            try:
                # Gets event and dispatches to handler.
                ev = self.get_event()
                # Only dispatches event if Event-Name header found.
                if ev and ev['Event-Name']:
                    self._spawn(self.dispatch_event, ev)
                gevent.sleep(0.0001)
            except (LimitExceededError, ConnectError, socket.error):
                self.connected = False
                break
            except GreenletExit, e:
                self.connected = False
                break
        return

    def read_event(self):
        '''
        Reads one Event from socket until EOL.

        Returns Event instance.

        Raises LimitExceededError if MAXLINES_PER_EVENT is reached.
        '''
        buff = ''
        for x in range(MAXLINES_PER_EVENT):
            line = self.transport.read_line()
            gevent.sleep(0.0001)
            if line == '':
                raise ConnectError("connection closed")
            elif line == EOL:
                # When matches EOL, creates Event and returns it.
                return Event(buff)
            else:
                # Else appends line to current buffer.
                buff += line
        raise LimitExceededError("max lines per event (%d) reached" % MAXLINES_PER_EVENT)

    def read_raw(self, event):
        '''
        Reads raw data based on Event Content-Length.

        Returns raw string or None if not found.
        '''
        length = event.get_content_length()
        # Reads length bytes if length > 0
        if length:
            res = self.transport.read(int(length))
            gevent.sleep(0.0001)
            return res
        return None

    def read_raw_response(self, event, raw):
        '''
        Extracts raw response from raw buffer and length based on Event Content-Length.

        Returns raw string or None if not found.
        '''
        length = event.get_content_length()
        if length:
            return raw[-length:]
        return None

    def get_event(self):
        '''
        Gets complete Event, and processes response callback.
        '''
        event = self.read_event()
        # Gets callback response for this event (sets to self._unknown_event, if no matching callable)
        _get_response = self._response_callbacks.get(event.get_content_type(), self._unknown_event)
        # If callback response found, starts this method to get final event.
        if _get_response:
            event = _get_response(event)
        return event

    def _auth_request(self, event):
        '''
        Receives auth/request callback.
        '''
        # Pushes Event to response events queue and returns Event.
        self._response_queue.put(event)
        return event

    def _api_response(self, event):
        '''
        Receives api/response callback.
        '''
        # Gets raw data for this event.
        raw = self.read_raw(event)
        # If raw was found, this is our Event body.
        if raw:
            event.set_body(raw)
        # Pushes Event to response events queue and returns Event.
        self._response_queue.put(event)
        return event

    def _command_reply(self, event):
        '''
        Receives command/reply callback.
        '''
        # Pushes Event to response events queue and returns Event.
        self._response_queue.put(event)
        return event

    def _event_plain(self, event):
        '''
        Receives text/event-plain callback.
        '''
        # Gets raw data for this event
        raw = self.read_raw(event)
        # If raw was found drops current event
        # and replaces with Event created from raw
        if raw:
            event = Event(raw)
            # Gets raw response from Event Content-Length header
            # and raw buffer
            raw_response = self.read_raw_response(event, raw)
            # If rawresponse was found, this is our Event body
            if raw_response:
                event.set_body(raw_response)
        # Returns Event
        return event

    def _event_json(self, event):
        '''
        Receives text/event-json callback.
        '''
        # Gets json data for this event
        json_data = self.read_raw(event)
        # If raw was found drops current event
        # and replaces with JsonEvent created from json_data
        if json_data:
            event = JsonEvent(json_data)
        # Returns Event
        return event

    def _disconnect_notice(self, event):
        '''
        Receives text/disconnect-notice callback.
        '''
        self._closing_state = True


    def _unknown_event(self, event):
        '''
        Receives unknown event type Callbacks.

        Can be implemented in subclass to process unknown event types.
        '''
        pass

    def dispatch_event(self, event):
        '''
        Dispatches one event with callback.

        E.g. Receives Background_Job event and calls on_background_job function.
        '''
        method = 'on_' + event['Event-Name'].lower()
        callback = getattr(self, method, None)
        # When no callbacks found, call unbound_event.
        if not callback:
            callback = self.unbound_event
        # Calls callback.
        try:
            callback(event)
        except:
            self.callback_failure(event)

    def callback_failure(self, event):
        '''
        Called when callback to an event fails.

        Can be implemented by the subclass.
        '''
        pass

    def unbound_event(self, event):
        '''
        Catches all unbound events from FreeSWITCH.

        Can be implemented by the subclass.
        '''
        pass

    def connect(self):
        '''
        Connects to eventsocket.
        '''
        self._closing_state = False

    def disconnect(self):
        '''
        Disconnect and release socket and finally kill event handler.
        '''
        try:
            self.transport.close()
        except:
            pass
        self._handler_thread.kill()
        # prevent any pending request to be stuck
        self._response_queue.put_nowait(Event())
        self.connected = False

    def _send(self, cmd):
        if isinstance(cmd, types.UnicodeType):
            cmd = cmd.encode("utf-8")
        self.transport.write(cmd + EOL*2)

    def _sendmsg(self, name, arg=None, uuid="", lock=False, loops=1):
        if isinstance(name, types.UnicodeType):
            name = name.encode("utf-8")
        if isinstance(arg, types.UnicodeType):
            arg = arg.encode("utf-8")
        msg = "sendmsg %s\ncall-command: execute\n" % uuid
        msg += "execute-app-name: %s\n" % name
        if lock is True:
            msg += "event-lock: true\n"
        if loops > 1:
            msg += "loops: %d\n" % loops
        if arg:
            arglen = len(arg)
            msg += "content-type: text/plain\ncontent-length: %d\n\n%s\n" % (arglen, arg)
        self.transport.write(msg + EOL)

    def _protocol_send(self, command, args=""):
        if self._closing_state:
            return Event()
        self._lock.acquire()
        try:
            self._send("%s %s" % (command, args))
            event = self._response_queue.get()
        finally:
            self._lock.release()
        # Casts Event to appropriate event type :
        # Casts to ApiResponse, if event is api
        if command == 'api':
            event = ApiResponse.cast(event)
        # Casts to BgapiResponse, if event is bgapi
        elif command == "bgapi":
            event = BgapiResponse.cast(event)
        # Casts to CommandResponse by default
        else:
            event = CommandResponse.cast(event)
        gevent.sleep(0.0001)
        return event

    def _protocol_sendmsg(self, name, args=None, uuid="", lock=False, loops=1):
        if self._closing_state:
            return Event()
        self._lock.acquire()
        try:
            self._sendmsg(name, args, uuid, lock, loops)
            event = self._response_queue.get()
        finally:
            self._lock.release()
        gevent.sleep(0.0001)
        # Always casts Event to CommandResponse
        return CommandResponse.cast(event)
