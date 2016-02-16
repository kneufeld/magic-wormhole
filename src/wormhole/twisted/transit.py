from __future__ import print_function
import sys, time, socket, collections
from binascii import hexlify, unhexlify
from zope.interface import implementer
from twisted.python.runtime import platformType
from twisted.internet import (reactor, interfaces, defer, protocol,
                              endpoints, task, address, error)
from twisted.protocols import policies
from nacl.secret import SecretBox
from ..util import ipaddrs
from ..util.hkdf import HKDF
from ..errors import UsageError
from ..transit_common import (BadHandshake,
                              BadNonce,
                              build_receiver_handshake,
                              build_sender_handshake,
                              build_relay_handshake)

def debug(msg):
    if False:
        print(msg)
def since(start):
    return time.time() - start

TIMEOUT=15

@implementer(interfaces.IProducer, interfaces.IConsumer)
class Connection(protocol.Protocol, policies.TimeoutMixin):
    def __init__(self, owner, relay_handshake, start):
        self.state = "too-early"
        self.buf = b""
        self.owner = owner
        self.relay_handshake = relay_handshake
        self.start = start
        self._negotiation_d = defer.Deferred(self._cancel)
        self._error = None
        self._consumer = None
        self._inbound_records = collections.deque()
        self._waiting_reads = collections.deque()

    def connectionMade(self):
        debug("handle %r" %  (self.transport,))
        self.setTimeout(TIMEOUT) # does timeoutConnection() when it expires
        self.factory.connectionWasMade(self, self.transport.getPeer())

    def startNegotiation(self, description):
        self.description = description
        if self.relay_handshake is not None:
            self.transport.write(self.relay_handshake)
            self.state = "relay"
        else:
            self.state = "start"
        self.dataReceived(b"") # cycle the state machine
        return self._negotiation_d

    def _cancel(self, d):
        self.state = "hung up" # stop reacting to anything further
        self._error = defer.CancelledError()
        self.transport.loseConnection()
        # if connectionLost isn't called synchronously, then our
        # self._negotiation_d will have been errbacked by Deferred.cancel
        # (which is our caller). So if it's still around, clobber it
        if self._negotiation_d:
            self._negotiation_d = None


    def dataReceived(self, data):
        try:
            self._dataReceived(data)
        except Exception as e:
            self.setTimeout(None)
            self._error = e
            self.transport.loseConnection()
            self.state = "hung up"
            if not isinstance(e, BadHandshake):
                raise

    def _check_and_remove(self, expected):
        # any divergence is a handshake error
        if not self.buf.startswith(expected[:len(self.buf)]):
            raise BadHandshake("got %r want %r" % (self.buf, expected))
        if len(self.buf) < len(expected):
            return False # keep waiting
        self.buf = self.buf[len(expected):]
        return True

    def _dataReceived(self, data):
        # protocol is:
        #  (maybe: send relay handshake, wait for ok)
        #  send (send|receive)_handshake
        #  wait for (receive|send)_handshake
        #  sender: decide, send "go" or hang up
        #  receiver: wait for "go"
        self.buf += data

        assert self.state != "too-early"
        if self.state == "relay":
            if not self._check_and_remove(b"ok\n"):
                return
            self.state = "start"
        if self.state == "start":
            self.transport.write(self.owner._send_this())
            self.state = "handshake"
        if self.state == "handshake":
            if not self._check_and_remove(self.owner._expect_this()):
                return
            self.state = self.owner.connection_ready(self, self.description)
            # If we're the receiver, we'll be moved to state
            # "wait-for-decision", which means we're waiting for the other
            # side (the sender) to make a decision. If we're the sender,
            # we'll either be moved to state "go" (send GO and move directly
            # to state "records") or state "nevermind" (send NEVERMIND and
            # hang up).

        if self.state == "wait-for-decision":
            if not self._check_and_remove(b"go\n"):
                return
            self._negotiationSuccessful()
        if self.state == "go":
            GO = b"go\n"
            self.transport.write(GO)
            self._negotiationSuccessful()
        if self.state == "nevermind":
            self.transport.write(b"nevermind\n")
            raise BadHandshake("abandoned")
        if self.state == "records":
            return self.dataReceivedRECORDS()
        if isinstance(self.state, Exception): # for tests
            raise self.state

    def _negotiationSuccessful(self):
        self.state = "records"
        self.setTimeout(None)
        send_key = self.owner._sender_record_key()
        self.send_box = SecretBox(send_key)
        self.send_nonce = 0
        receive_key = self.owner._receiver_record_key()
        self.receive_box = SecretBox(receive_key)
        self.next_receive_nonce = 0
        d, self._negotiation_d = self._negotiation_d, None
        d.callback(self)

    def dataReceivedRECORDS(self):
        if len(self.buf) < 4:
            return
        length = int(hexlify(self.buf[:4]), 16)
        if len(self.buf) < 4+length:
            return
        encrypted, self.buf = self.buf[4:4+length], self.buf[4+length:]

        record = self._decrypt_record(encrypted)
        self.recordReceived(record)

    def _decrypt_record(self, encrypted):
        nonce_buf = encrypted[:SecretBox.NONCE_SIZE] # assume it's prepended
        nonce = int(hexlify(nonce_buf), 16)
        if nonce != self.next_receive_nonce:
            raise BadNonce("received out-of-order record: got %d, expected %d"
                           % (nonce, self.next_receive_nonce))
        self.next_receive_nonce += 1
        record = self.receive_box.decrypt(encrypted)
        return record

    def send_record(self, record):
        if not isinstance(record, type(b"")): raise UsageError
        assert SecretBox.NONCE_SIZE == 24
        assert self.send_nonce < 2**(8*24)
        assert len(record) < 2**(8*4)
        nonce = unhexlify("%048x" % self.send_nonce) # big-endian
        self.send_nonce += 1
        encrypted = self.send_box.encrypt(record, nonce)
        length = unhexlify("%08x" % len(encrypted)) # always 4 bytes long
        self.transport.write(length)
        self.transport.write(encrypted)

    def recordReceived(self, record):
        if self._consumer:
            self._consumer.write(record)
            return
        self._inbound_records.append(record)
        self._deliverRecords()

    def receive_record(self):
        d = defer.Deferred()
        self._waiting_reads.append(d)
        self._deliverRecords()
        return d

    def _deliverRecords(self):
        while self._inbound_records and self._waiting_reads:
            r = self._inbound_records.popleft()
            d = self._waiting_reads.popleft()
            d.callback(r)

    def close(self):
        self.transport.loseConnection()
        while self._waiting_reads:
            d = self._waiting_reads.popleft()
            d.errback(error.ConnectionClosed())

    def timeoutConnection(self):
        self._error = BadHandshake("timeout")
        self.transport.loseConnection()

    def connectionLost(self, reason=None):
        self.setTimeout(None)
        d, self._negotiation_d = self._negotiation_d, None
        # the Deferred is only relevant until negotiation finishes, so skip
        # this if it's alredy been fired
        if d:
            # Each call to loseConnection() sets self._error first, so we can
            # deliver useful information to the Factory that's waiting on
            # this (although they'll generally ignore the specific error,
            # except for logging unexpected ones). The possible cases are:
            #
            # cancel: defer.CancelledError
            # far-end disconnect: BadHandshake("connection lost")
            # handshake error (something we didn't like): BadHandshake(what)
            # other error: some other Exception
            # timeout: BadHandshake("timeout")

            d.errback(self._error or BadHandshake("connection lost"))

    # IConsumer methods, for outbound flow-control. We pass these through to
    # the transport. The 'producer' is something like a t.p.basic.FileSender
    def registerProducer(self, producer, streaming):
        assert interfaces.IConsumer.providedBy(self.transport)
        self.transport.registerProducer(producer, streaming)
    def unregisterProducer(self):
        self.transport.unregisterProducer()
    def write(self, data):
        self.send_record(data)

    # IProducer methods, for inbound flow-control. We pass these through to
    # the transport.
    def stopProducing(self):
        self.transport.stopProducing()
    def pauseProducing(self):
        self.transport.pauseProducing()
    def resumeProducing(self):
        self.transport.resumeProducing()

    # Helper method to glue an instance of e.g. t.p.ftp.FileConsumer to us.
    # Inbound records will be written as bytes to the consumer.
    def connectConsumer(self, consumer):
        if self._consumer:
            raise RuntimeError("A consumer is already attached: %r" %
                               self._consumer)
        self._consumer = consumer
        # drain any pending records
        while self._inbound_records:
            r = self._inbound_records.popleft()
            consumer.write(r)
        consumer.registerProducer(self, True)

    def disconnectConsumer(self):
        self._consumer.unregisterProducer()
        self._consumer = None

class OutboundConnectionFactory(protocol.ClientFactory):
    protocol = Connection

    def __init__(self, owner, relay_handshake):
        self.owner = owner
        self.relay_handshake = relay_handshake
        self.start = time.time()

    def buildProtocol(self, addr):
        p = self.protocol(self.owner, self.relay_handshake, self.start)
        p.factory = self
        return p

    def connectionWasMade(self, p, addr):
        # outbound connections are handled via the endpoint
        pass


class InboundConnectionFactory(protocol.ClientFactory):
    protocol = Connection

    def __init__(self, owner):
        self.owner = owner
        self.start = time.time()
        self._inbound_d = defer.Deferred(self._cancel)
        self._pending_connections = set()

    def whenDone(self):
        return self._inbound_d

    def _cancel(self, inbound_d):
        self._shutdown()
        # our _inbound_d will be errbacked by Deferred.cancel()

    def _shutdown(self):
        for d in list(self._pending_connections):
            d.cancel() # that fires _remove and _proto_failed

    def describePeer(self, addr):
        if isinstance(addr, address.HostnameAddress):
            return "<-%s:%d" % (addr.hostname, addr.port)
        elif isinstance(addr, (address.IPv4Address, address.IPv6Address)):
            return "<-%s:%d" % (addr.host, addr.port)
        return "<-%r" % addr

    def buildProtocol(self, addr):
        p = self.protocol(self.owner, None, self.start)
        p.factory = self
        return p

    def connectionWasMade(self, p, addr):
        d = p.startNegotiation(self.describePeer(addr))
        self._pending_connections.add(d)
        d.addBoth(self._remove, d)
        d.addCallbacks(self._proto_succeeded, self._proto_failed)

    def _remove(self, res, d):
        self._pending_connections.remove(d)
        return res

    def _proto_succeeded(self, p):
        self._shutdown()
        self._inbound_d.callback(p)

    def _proto_failed(self, f):
        # ignore these two, let Twisted log everything else
        f.trap(BadHandshake, defer.CancelledError)
        pass

def allocate_tcp_port():
    """Return an (integer) available TCP port on localhost. This briefly
    listens on the port in question, then closes it right away."""
    # We want to bind() the socket but not listen(). Twisted (in
    # tcp.Port.createInternetSocket) would do several other things:
    # non-blocking, close-on-exec, and SO_REUSEADDR. We don't need
    # non-blocking because we never listen on it, and we don't need
    # close-on-exec because we close it right away. So just add SO_REUSEADDR.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if platformType == "posix" and sys.platform != "cygwin":
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port

class _ThereCanBeOnlyOne:
    """Accept a list of contender Deferreds, and return a summary Deferred.
    When the first contender fires successfully, cancel the rest and fire the
    summary with the winning contender's result. If all error, errback the
    summary.

    status_cb=?
    """
    def __init__(self, contenders):
        self._remaining = set(contenders)
        self._winner_d = defer.Deferred(self._cancel)
        self._first_success = None
        self._first_failure = None
        self._have_winner = False
        self._fired = False

    def _cancel(self, _):
        for d in list(self._remaining):
            d.cancel()
        # since that will errback everything in _remaining, we'll have hit
        # _maybe_done() and fired self._winner_d by this point

    def run(self):
        for d in list(self._remaining):
            d.addBoth(self._remove, d)
            d.addCallbacks(self._succeeded, self._failed)
            d.addCallback(self._maybe_done)
        return self._winner_d

    def _remove(self, res, d):
        self._remaining.remove(d)
        return res

    def _succeeded(self, res):
        self._have_winner = True
        self._first_success = res
        for d in list(self._remaining):
            d.cancel()

    def _failed(self, f):
        if self._first_failure is None:
            self._first_failure = f

    def _maybe_done(self, _):
        if self._remaining:
            return
        if self._fired:
            return
        self._fired = True
        if self._have_winner:
            self._winner_d.callback(self._first_success)
        else:
            self._winner_d.errback(self._first_failure)

def there_can_be_only_one(contenders):
    return _ThereCanBeOnlyOne(contenders).run()

class Common:
    RELAY_DELAY = 2.0

    def __init__(self, transit_relay, reactor=reactor):
        if not isinstance(transit_relay, (type(None), type(u""))):
            raise UsageError
        self._transit_relay = transit_relay
        self._transit_key = None
        self._waiting_for_transit_key = []
        self._listener = None
        self._winner = None
        self._winner_description = None
        self._reactor = reactor

    def _build_listener(self):
        portnum = allocate_tcp_port()
        direct_hints = [u"tcp:%s:%d" % (addr, portnum)
                        for addr in ipaddrs.find_addresses()]
        ep = endpoints.serverFromString(reactor, "tcp:%d" % portnum)
        return direct_hints, ep

    def get_direct_hints(self):
        if self._listener:
            return defer.succeed(self._my_direct_hints)
        # there is a slight race here: if someone calls get_direct_hints() a
        # second time, before the listener has actually started listening,
        # then they'll get a Deferred that fires (with the hints) before the
        # listener starts listening. But most applications won't call this
        # multiple times, and the race is between 1: the parent Wormhole
        # protocol getting the connection hints to the other end, and 2: the
        # listener being ready for connections, and I'm confident that the
        # listener will win.
        self._my_direct_hints, self._listener = self._build_listener()

        # Start the server, so it will be running by the time anyone tries to
        # connect to the direct hints we return.
        f = InboundConnectionFactory(self)
        self._listener_f = f # for tests # XX move to __init__ ?
        self._listener_d = f.whenDone()
        d = self._listener.listen(f)
        def _listening(lp):
            # lp is an IListeningPort
            #self._listener_port = lp # for tests
            def _stop_listening(res):
                lp.stopListening()
                return res
            self._listener_d.addBoth(_stop_listening)
            return self._my_direct_hints
        d.addCallback(_listening)
        return d

    def _stop_listening(self):
        # this is for unit tests. The usual control flow (via connect())
        # wires the listener's Deferred into a there_can_be_only_one(), which
        # eats the errback. If we don't ever call connect(), we must catch it
        # ourselves.
        self._listener_d.addErrback(lambda f: None)
        self._listener_d.cancel()

    def get_relay_hints(self):
        if self._transit_relay:
            return [self._transit_relay]
        return []

    def add_their_direct_hints(self, hints):
        for h in hints:
            if not isinstance(h, type(u"")):
                raise TypeError("hint '%r' should be unicode, not %s"
                                % (h, type(h)))
        self._their_direct_hints = set(hints)
    def add_their_relay_hints(self, hints):
        for h in hints:
            if not isinstance(h, type(u"")):
                raise TypeError("hint '%r' should be unicode, not %s"
                                % (h, type(h)))
        self._their_relay_hints = set(hints)

    def _send_this(self):
        if self.is_sender:
            return build_sender_handshake(self._transit_key)
        else:
            return build_receiver_handshake(self._transit_key)

    def _expect_this(self):
        if self.is_sender:
            return build_receiver_handshake(self._transit_key)
        else:
            return build_sender_handshake(self._transit_key)# + b"go\n"

    def _sender_record_key(self):
        if self.is_sender:
            return HKDF(self._transit_key, SecretBox.KEY_SIZE,
                        CTXinfo=b"transit_record_sender_key")
        else:
            return HKDF(self._transit_key, SecretBox.KEY_SIZE,
                        CTXinfo=b"transit_record_receiver_key")

    def _receiver_record_key(self):
        if self.is_sender:
            return HKDF(self._transit_key, SecretBox.KEY_SIZE,
                        CTXinfo=b"transit_record_receiver_key")
        else:
            return HKDF(self._transit_key, SecretBox.KEY_SIZE,
                        CTXinfo=b"transit_record_sender_key")

    def set_transit_key(self, key):
        # We use pubsub to protect against the race where the sender knows
        # the hints and the key, and connects to the receiver's transit
        # socket before the receiver gets the relay message (and thus the
        # key).
        self._transit_key = key
        waiters = self._waiting_for_transit_key
        del self._waiting_for_transit_key
        for d in waiters:
            # We don't need eventual-send here. It's safer in general, but
            # set_transit_key() is only called once, and _get_transit_key()
            # won't touch the subscribers list once the key is set.
            d.callback(key)

    def _get_transit_key(self):
        if self._transit_key:
            return defer.succeed(self._transit_key)
        d = defer.Deferred()
        self._waiting_for_transit_key.append(d)
        return d

    def connect(self):
        d = self._get_transit_key()
        d.addCallback(self._connect)
        # we want to have the transit key before starting any outbound
        # connections, so those connections will know what to say when they
        # connect
        return d

    def _connect(self, _):
        # It might be nice to wire this so that a failure in the direct hints
        # causes the relay hints to be used right away (fast failover). But
        # none of our current use cases would take advantage of that: if we
        # have any viable direct hints, then they're either going to succeed
        # quickly or hang for a long time.
        contenders = []
        contenders.append(self._listener_d)
        relay_delay = 0

        for hint in self._their_direct_hints:
            # Check the hint type to see if we can support it (e.g. skip
            # onion hints on a non-Tor client). Do not increase relay_delay
            # unless we have at least one viable hint.
            ep = self._endpoint_from_hint(hint)
            if not ep:
                continue
            description = "->%s" % (hint,)
            d = self._start_connector(ep, description)
            contenders.append(d)
            relay_delay = self.RELAY_DELAY

        # Start trying the relay a few seconds after we start to try the
        # direct hints. The idea is to prefer direct connections, but not be
        # afraid of using the relay when we have direct hints that don't
        # resolve quickly. Many direct hints will be to unused local-network
        # IP addresses, which won't answer, and would take the full TCP
        # timeout (30s or more) to fail.
        for hint in self._their_relay_hints:
            ep = self._endpoint_from_hint(hint)
            if not ep:
                continue
            description = "->relay:%s" % (hint,)
            d = task.deferLater(self._reactor, relay_delay,
                                self._start_connector, ep, description,
                                is_relay=True)
            contenders.append(d)

        winner = there_can_be_only_one(contenders)
        return self._not_forever(2*TIMEOUT, winner)

    def _not_forever(self, timeout, d):
        """If the timer fires first, cancel the deferred. If the deferred fires
        first, cancel the timer."""
        t = self._reactor.callLater(timeout, d.cancel)
        def _done(res):
            if t.active():
                t.cancel()
            return res
        d.addBoth(_done)
        return d

    def _start_connector(self, ep, description, is_relay=False):
        relay_handshake = None
        if is_relay:
            relay_handshake = build_relay_handshake(self._transit_key)
        f = OutboundConnectionFactory(self, relay_handshake)
        d = ep.connect(f)
        # fires with protocol, or ConnectError
        d.addCallback(lambda p: p.startNegotiation(description))
        return d

    def _endpoint_from_hint(self, hint):
        # TODO: use transit_common.parse_hint_tcp
        if ":" not in hint:
            return None
        hint_type = hint.split(":")[0]
        if hint_type != "tcp":
            return None
        pieces = hint.split(":")
        return endpoints.HostnameEndpoint(self._reactor, pieces[1],
                                          int(pieces[2]))

    def connection_ready(self, p, description):
        # inbound/outbound Connection protocols call this when they finish
        # negotiation. The first one wins and gets a "go". Any subsequent
        # ones lose and get a "nevermind" before being closed.

        if not self.is_sender:
            return "wait-for-decision"

        if self._winner:
            # we already have a winner, so this one loses
            return "nevermind"
        # this one wins!
        self._winner = p
        self._winner_description = description
        return "go"

    def describe(self):
        if not self._winner:
            return "not yet established"
        return self._winner_description

class TransitSender(Common):
    is_sender = True

class TransitReceiver(Common):
    is_sender = False

# the TransitSender/Receiver.connect() yields a Connection, on which you can
# do send_record(), but what should the receive API be? set a callback for
# inbound records? get a Deferred for the next record? The producer/consumer
# API is enough for file transfer, but what would other applications want?

# how should the Listener be managed? we want to shut it down when the
# connect() Deferred is cancelled, as well as terminating any negotiations in
# progress.
#
# the factory should return/manage a deferred, which fires iff an inbound
# connection completes negotiation successfully, can be cancelled (which
# stops the listener and drops all pending connections), but will never
# timeout, and only errbacks if cancelled.

# write unit test for _ThereCanBeOnlyOne

# check start/finish time-gathering instrumentation

# add progress API

# relay URLs are probably mishandled: both sides probably send their URL,
# then connect to the *other* side's URL, when they really should connect to
# both their own and the other side's. The current implementation probably
# only works if the two URLs are the same.