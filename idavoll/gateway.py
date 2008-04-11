# -*- test-case-name: idavoll.test.test_gateway -*-
#
# Copyright (c) 2003-2008 Ralph Meijer
# See LICENSE for details.

"""
Web resources and client for interacting with pubsub services.
"""

import cgi
from time import gmtime, strftime
import urllib
import urlparse

import simplejson

from twisted.application import service
from twisted.internet import defer, reactor
from twisted.python import log
from twisted.web import client
from twisted.web2 import http, http_headers, resource, responsecode
from twisted.web2 import channel, server
from twisted.web2.stream import readStream
from twisted.words.protocols.jabber.jid import JID
from twisted.words.protocols.jabber.error import StanzaError
from twisted.words.xish import domish

from wokkel.pubsub import Item
from wokkel.pubsub import PubSubClient

from idavoll import error

NS_ATOM = 'http://www.w3.org/2005/Atom'
MIME_ATOM_ENTRY = 'application/atom+xml;type=entry'
MIME_JSON = 'application/json'

class RemoteSubscriptionService(service.Service, PubSubClient):

    def __init__(self, jid):
        self.jid = jid

    def startService(self):
        self.callbacks = {}

    def trapNotFound(self, failure):
        failure.trap(StanzaError)
        if not failure.value.condition == 'item-not-found':
            raise failure
        raise error.NodeNotFound

    def subscribeCallback(self, jid, nodeIdentifier, callback):

        def newCallbackList(result):
            callbackList = set()
            self.callbacks[jid, nodeIdentifier] = callbackList
            return callbackList

        try:
            callbackList = self.callbacks[jid, nodeIdentifier]
        except KeyError:
            d = self.subscribe(jid, nodeIdentifier, self.jid)
            d.addCallback(newCallbackList)
        else:
            d = defer.succeed(callbackList)

        d.addCallback(lambda callbackList: callbackList.add(callback))
        d.addErrback(self.trapNotFound)
        return d

    def unsubscribeCallback(self, jid, nodeIdentifier, callback):
        try:
            callbackList = self.callbacks[jid, nodeIdentifier]
            callbackList.remove(callback)
        except KeyError:
            return defer.fail(error.NotSubscribed())

        if not callbackList:
            self.unsubscribe(jid, nodeIdentifier, self.jid)

        return defer.succeed(None)

    def itemsReceived(self, recipient, service, nodeIdentifier, items):
        """
        Fire up HTTP client to do callback
        """

        # Collect atom entries
        atomEntries = []
        for item in items:
            # ignore non-items (i.e. retractions)
            if item.name != 'item':
                continue

            atomEntry = None
            for element in item.elements():
                # extract the first element that is an atom entry
                if element.uri == NS_ATOM and element.name == 'entry':
                    atomEntry = element
                    break

            if atomEntry:
                atomEntries.append(atomEntry)

        # Don't notify if there are no atom entries
        if not atomEntries:
            return

        if len(atomEntries) == 1:
            contentType = 'application/atom+xml;type=entry'
            payload = atomEntries[0]
        else:
            contentType = 'application/atom+xml;type=feed'
            nodeURI = 'xmpp:%s?;node=%s' % (service.full(), nodeIdentifier)
            now = strftime("%Y-%m-%dT%H:%M:%SZ", gmtime())

            # Collect the received entries in a feed
            payload = domish.Element((NS_ATOM, 'feed'))
            payload.addElement('title', content='Received item collection')
            payload.addElement('id', content=nodeURI)
            payload.addElement('updated', content=now)
            for atomEntry in atomEntries:
                payload.addChild(atomEntry)

        self.callCallbacks(recipient, service, nodeIdentifier, payload,
                           contentType)

    def deleteReceived(self, recipient, service, nodeIdentifier):
        """
        Fire up HTTP client to do callback
        """

        self.callCallbacks(recipient, service, nodeIdentifier,
                           eventType='DELETED')

    def callCallbacks(self, recipient, service, nodeIdentifier,
                            payload=None, contentType=None, eventType=None):
        try:
            callbacks = self.callbacks[service, nodeIdentifier]
        except KeyError:
            return

        postdata = None
        nodeURI = 'xmpp:%s?;node=%s' % (service.full(), nodeIdentifier)
        headers = {'Referer': nodeURI.encode('utf-8'),
                   'PubSub-Service': service.full().encode('utf-8')}

        if payload:
            postdata = payload.toXml().encode('utf-8')
            if contentType:
                headers['Content-Type'] = "%s;charset=utf-8" % contentType

        if eventType:
            headers['Event'] = eventType

        def postNotification(callbackURI):
            d = client.getPage(str(callbackURI),
                                   method='POST',
                                   postdata=postdata,
                                   headers=headers)
            d.addErrback(log.err)

        for callbackURI in callbacks:
            reactor.callLater(0, postNotification, callbackURI)


class WebStreamParser(object):
    def __init__(self):
        self.elementStream = domish.elementStream()
        self.elementStream.DocumentStartEvent = self.docStart
        self.elementStream.ElementEvent = self.elem
        self.elementStream.DocumentEndEvent = self.docEnd
        self.done = False

    def docStart(self, elem):
        self.document = elem

    def elem(self, elem):
        self.document.addChild(elem)

    def docEnd(self):
        self.done = True

    def parse(self, stream):
        def endOfStream(result):
            if not self.done:
                raise Exception("No more stuff?")
            else:
                return self.document

        d = readStream(stream, self.elementStream.parse)
        d.addCallback(endOfStream)
        return d


class XMPPURIParseError(ValueError):
    """
    Raised when a given XMPP URI couldn't be properly parsed.
    """


def getServiceAndNode(uri):
    """
    Given an XMPP URI, extract the publish subscribe service JID and node ID.
    """

    try:
        scheme, rest = uri.split(':', 1)
    except ValueError:
        raise XMPPURIParseError("No URI scheme component")

    if scheme != 'xmpp':
        raise XMPPURIParseError("Unknown URI scheme")

    if rest.startswith("//"):
        raise XMPPURIParseError("Unexpected URI authority component")

    try:
        entity, query = rest.split('?', 1)
    except ValueError:
        raise XMPPURIParseError("No URI query component")

    if not entity:
        raise XMPPURIParseError("Empty URI path component")

    try:
        jid = JID(entity)
    except Exception, e:
        raise XMPPURIParseError("Invalid JID: %s" % e.message)

    params = cgi.parse_qs(query)

    try:
        nodeIdentifier = params['node'][0]
    except (KeyError, ValueError):
        raise XMPPURIParseError("No node in query component of URI")

    return jid, nodeIdentifier


class CreateResource(resource.Resource):
    """
    A resource to create a publish-subscribe node.
    """
    def __init__(self, backend, serviceJID, owner):
        self.backend = backend
        self.serviceJID = serviceJID
        self.owner = owner

    http_GET = None

    def http_POST(self, request):
        """
        Respond to a POST request to create a new node.
        """

        def toResponse(nodeIdentifier):
            uri = 'xmpp:%s?;node=%s' % (self.serviceJID.full(), nodeIdentifier)
            stream = simplejson.dumps({'uri': uri})
            return http.Response(responsecode.OK, stream=stream)

        d = self.backend.create_node(None, self.owner)
        d.addCallback(toResponse)
        return d


class DeleteResource(resource.Resource):
    """
    A resource to create a publish-subscribe node.
    """
    def __init__(self, backend, serviceJID, owner):
        self.backend = backend
        self.serviceJID = serviceJID
        self.owner = owner

    http_GET = None

    def http_POST(self, request):
        """
        Respond to a POST request to create a new node.
        """

        def respond(result):
            return http.Response(responsecode.NO_CONTENT)

        def getNode():
            if request.args.get('uri'):
                jid, nodeIdentifier = getServiceAndNode(request.args['uri'][0])
                return defer.succeed(nodeIdentifier)
            else:
                raise http.HTTPError(http.Response(responsecode.BAD_REQUEST,
                                                   "No URI given"))

        def trapNotFound(failure):
            failure.trap(error.NodeNotFound)
            return http.StatusResponse(responsecode.NOT_FOUND,
                                       "Node not found")

        def trapXMPPURIParseError(failure):
            failure.trap(XMPPURIParseError)
            return http.StatusResponse(responsecode.BAD_REQUEST,
                    "Malformed XMPP URI: %s" % failure.value.message)

        d = getNode()
        d.addCallback(self.backend.delete_node, self.owner)
        d.addCallback(respond)
        d.addErrback(trapNotFound)
        d.addErrback(trapXMPPURIParseError)
        return d


class PublishResource(resource.Resource):
    """
    A resource to publish to a publish-subscribe node.
    """

    def __init__(self, backend, serviceJID, owner):
        self.backend = backend
        self.serviceJID = serviceJID
        self.owner = owner

    http_GET = None

    def checkMediaType(self, request):
        ctype = request.headers.getHeader('content-type')

        if not ctype:
            raise http.HTTPError(
                http.StatusResponse(
                    responsecode.BAD_REQUEST,
                    "No specified Media Type"))

        if (ctype.mediaType != 'application' or
            ctype.mediaSubtype != 'atom+xml' or
            ctype.params.get('type') != 'entry' or
            ctype.params.get('charset', 'utf-8') != 'utf-8'):
            raise http.HTTPError(
                http.StatusResponse(
                    responsecode.UNSUPPORTED_MEDIA_TYPE,
                    "Unsupported Media Type: %s" %
                        http_headers.generateContentType(ctype)))

    def parseXMLPayload(self, stream):
        p = WebStreamParser()
        return p.parse(stream)

    def http_POST(self, request):
        """
        Respond to a POST request to create a new item.
        """

        def toResponse(nodeIdentifier):
            uri = 'xmpp:%s?;node=%s' % (self.serviceJID.full(), nodeIdentifier)
            stream = simplejson.dumps({'uri': uri})
            return http.Response(responsecode.OK, stream=stream)

        def gotNode(nodeIdentifier, payload):
            item = Item(id='current', payload=payload)
            d = self.backend.publish(nodeIdentifier, [item], self.owner)
            d.addCallback(lambda _: nodeIdentifier)
            return d

        def getNode():
            if request.args.get('uri'):
                jid, nodeIdentifier = getServiceAndNode(request.args['uri'][0])
                return defer.succeed(nodeIdentifier)
            else:
                return self.backend.create_node(None, self.owner)

        def doPublish(payload):
            d = getNode()
            d.addCallback(gotNode, payload)
            return d

        def trapNotFound(failure):
            failure.trap(error.NodeNotFound)
            return http.StatusResponse(responsecode.NOT_FOUND,
                                       "Node not found")

        def trapXMPPURIParseError(failure):
            failure.trap(XMPPURIParseError)
            return http.StatusResponse(responsecode.BAD_REQUEST,
                    "Malformed XMPP URI: %s" % failure.value.message)

        self.checkMediaType(request)
        d = self.parseXMLPayload(request.stream)
        d.addCallback(doPublish)
        d.addCallback(toResponse)
        d.addErrback(trapNotFound)
        d.addErrback(trapXMPPURIParseError)
        return d


class SubscribeBaseResource(resource.Resource):
    """
    Base resource for remote pubsub node subscription and unsubscription.

    This resource accepts POST request with a JSON document that holds
    a dictionary with the keys C{uri} and C{callback} that respectively map
    to the XMPP URI of the publish-subscribe node and the callback URI.

    This class should be inherited with L{serviceMethod} overridden.

    @cvar serviceMethod: The name of the method to be called with
                         the JID of the pubsub service, the node identifier
                         and the callback URI as received in the HTTP POST
                         request to this resource.
    """
    serviceMethod = None

    def __init__(self, service):
        self.service = service
        self.params = None

    http_GET = None

    def http_POST(self, request):
        def trapNotFound(failure):
            failure.trap(error.NodeNotFound)
            return http.StatusResponse(responsecode.NOT_FOUND,
                                       "Node not found")

        def respond(result):
            return http.Response(responsecode.NO_CONTENT)

        def gotRequest(result):
            uri = self.params['uri']
            callback = self.params['callback']

            jid, nodeIdentifier = getServiceAndNode(uri)
            method = getattr(self.service, self.serviceMethod)
            d = method(jid, nodeIdentifier, callback)
            return d

        def storeParams(data):
            self.params = simplejson.loads(data)

        def trapXMPPURIParseError(failure):
            failure.trap(XMPPURIParseError)
            return http.StatusResponse(responsecode.BAD_REQUEST,
                    "Malformed XMPP URI: %s" % failure.value.message)

        d = readStream(request.stream, storeParams)
        d.addCallback(gotRequest)
        d.addCallback(respond)
        d.addErrback(trapNotFound)
        d.addErrback(trapXMPPURIParseError)
        return d


class SubscribeResource(SubscribeBaseResource):
    """
    Resource to subscribe to a remote publish-subscribe node.

    The passed C{uri} is the XMPP URI of the node to subscribe to and the
    C{callback} is the callback URI. Upon receiving notifications from the
    node, a POST request will be perfomed on the callback URI.
    """
    serviceMethod = 'subscribeCallback'


class UnsubscribeResource(SubscribeBaseResource):
    """
    Resource to unsubscribe from a remote publish-subscribe node.

    The passed C{uri} is the XMPP URI of the node to unsubscribe from and the
    C{callback} is the callback URI that was registered for it.
    """
    serviceMethod = 'unsubscribeCallback'


class ListResource(resource.Resource):
    def __init__(self, service):
        self.service = service

    def render(self, request):
        def responseFromNodes(nodeIdentifiers):
            return http.Response(responsecode.OK,
                                 stream=simplejson.dumps(nodeIdentifiers))

        d = self.service.get_nodes()
        d.addCallback(responseFromNodes)
        return d


def getPageWithFactory(url, contextFactory=None, *args, **kwargs):
    """Download a web page.

    Download a page. Return the factory that holds a deferred, which will
    callback with a page (as a string) or errback with a description of the
    error.

    See HTTPClientFactory to see what extra args can be passed.
    """

    scheme, host, port, path = client._parse(url)
    factory = client.HTTPClientFactory(url, *args, **kwargs)
    factory.protocol.handleStatus_204 = lambda self: self.handleStatus_200()

    if scheme == 'https':
        from twisted.internet import ssl
        if contextFactory is None:
            contextFactory = ssl.ClientContextFactory()
        reactor.connectSSL(host, port, factory, contextFactory)
    else:
        reactor.connectTCP(host, port, factory)
    return factory


class CallbackResource(resource.Resource):
    """
    Web resource for retrieving gateway notifications.
    """

    def __init__(self, callback):
        self.callback = callback

    http_GET = None

    def http_POST(self, request):
        p = WebStreamParser()
        d = p.parse(request.stream)
        d.addCallback(self.callback, request.headers)
        d.addCallback(lambda _: http.Response(responsecode.NO_CONTENT))
        return d

class GatewayClient(service.Service):
    """
    Service that provides client access to the HTTP Gateway into Idavoll.
    """

    agent = "Idavoll HTTP Gateway Client"

    def __init__(self, baseURI, callbackHost=None, callbackPort=None):
        self.baseURI = baseURI
        self.callbackHost = callbackHost or 'localhost'
        self.callbackPort = callbackPort or 8087
        root = resource.Resource()
        root.child_callback = CallbackResource(lambda *args, **kwargs: self.callback(*args, **kwargs))
        self.site = server.Site(root)

    def startService(self):
        self.port = reactor.listenTCP(self.callbackPort,
                                      channel.HTTPFactory(self.site))

    def stopService(self):
        return self.port.stopListening()

    def _makeURI(self, verb, query=None):
        uriComponents = urlparse.urlparse(self.baseURI)
        uri = urlparse.urlunparse((uriComponents[0],
                                   uriComponents[1],
                                   uriComponents[2] + verb,
                                   '',
                                   query and urllib.urlencode(query) or '',
                                   ''))
        return uri

    def callback(self, data, headers):
        pass

    def create(self):
        f = getPageWithFactory(self._makeURI('create'),
                    method='POST',
                    agent=self.agent)
        return f.deferred.addCallback(simplejson.loads)

    def publish(self, entry, xmppURI=None):
        query = xmppURI and {'uri': xmppURI}

        f = getPageWithFactory(self._makeURI('publish', query),
                    method='POST',
                    postdata=entry.toXml().encode('utf-8'),
                    headers={'Content-Type': MIME_ATOM_ENTRY},
                    agent=self.agent)
        return f.deferred.addCallback(simplejson.loads)

    def listNodes(self):
        f = getPageWithFactory(self._makeURI('list'),
                    method='GET',
                    agent=self.agent)
        return f.deferred.addCallback(simplejson.loads)

    def subscribe(self, xmppURI):
        params = {'uri': xmppURI,
                  'callback': 'http://%s:%s/callback' % (self.callbackHost,
                                                         self.callbackPort)}
        f = getPageWithFactory(self._makeURI('subscribe'),
                    method='POST',
                    postdata=simplejson.dumps(params),
                    headers={'Content-Type': MIME_JSON},
                    agent=self.agent)
        return f.deferred
