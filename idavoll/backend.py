from twisted.python import components
from twisted.application import service
from twisted.xish import utility

class Error(Exception):
    msg = ''

    def __str__(self):
        return self.msg
    
class NodeNotFound(Error):
    msg = 'Node not found'

class NotAuthorized(Error):
    pass

class PayloadExpected(Error):
    msg = 'Payload expected'

class NoPayloadAllowed(Error):
    msg = 'No payload allowed'

class NoInstantNodes(Error):
    pass

class NodeExists(Error):
    pass

class NotImplemented(Error):
    msg = 'Oops!'

class IBackendService(components.Interface):
    """ Interface to a backend service of a pubsub service. """

    def get_supported_affiliations(self):
        """ Reports the list of supported affiliation types.
    
        @return: a list of supported affiliation types.
        """

class INodeCreationService(components.Interface):
    """ A service for creating nodes """

    def create_node(self, node_id, requestor):
        """ Create a node.
        
        @return: a deferred that fires when the node has been created.
        """

class IPublishService(components.Interface):
    """ A service for publishing items to a node. """

    def publish(self, node_id, items, requestor):
        """ Publish items to a pubsub node.
        
        @return: a deferred that fires when the items have been published.
        """
class INotificationService(components.Interface):
    """ A service for notification of published items. """

    def register_notifier(self, observerfn, *args, **kwargs):
        """ Register callback which is called for notification. """

    def get_notification_list(self, node_id, items):
        pass

class ISubscriptionService(components.Interface):
    """ A service for managing subscriptions. """

    def subscribe(self, node_id, subscriber, requestor):
        """ Request the subscription of an entity to a pubsub node.

        Depending on the node's configuration and possible business rules, the
        C{subscriber} is added to the list of subscriptions of the node with id
        C{node_id}. The C{subscriber} might be different from the C{requestor},
        and if the C{requestor} is not allowed to subscribe this entity an
        exception should be raised.
    
        @return: a deferred that returns the subscription state
        """

    def unsubscribe(self, node_id, subscriber, requestor):
        """ Cancel the subscription of an entity to a pubsub node.

        The subscription of C{subscriber} is removed from the list of
        subscriptions of the node with id C{node_id}. If the C{requestor}
        is not allowed to unsubscribe C{subscriber}, an an exception should
        be raised.

        @return: a deferred that fires when unsubscription is complete.
        """

class IAffiliationsService(components.Interface):
    """ A service for retrieving the affiliations with this pubsub service. """

    def get_affiliations(self, entity):
        """ Report the list of current affiliations with this pubsub service.

        Report the list of the current affiliations with all nodes within this
        pubsub service, along with subscriptions to such nodes, for the
        C{entity}.

        @return: a deferred that returns the list of all current affiliations
        and subscriptions.
        """

class IPersistenceService(components.Interface):
    """ A service for persisting published items """

    def store_items(self, node_id, items, publisher):
        """ Store items for a node, recording the publisher """

class IRetractionService(components.Interface):
    """ A service for retracting published items """

    def retract_item(self, node_id, item_id, requestor):
        """ Removes item in node from persistent storage """

    def purge_node(self, node_id, requestor):
        """ Removes all items in node from persistent storage """

class IItemRetrievalService(components.Interface):
    """ A service for retrieving previously published items. """

    def get_items(self, node_id, max_items=None, item_ids=[],
                  requestor=None):
        """ Retrieve items from persistent storage

        If C{max_items} is given, return the C{max_items} last published
        items, else if C{item_ids} is not empty, return the items requested.
        If neither is given, return all items.

        @return: a deferred that returns the requested items
        """

class BackendService(service.MultiService, utility.EventDispatcher):

    __implements__ = IBackendService,

    def __init__(self):
        service.MultiService.__init__(self)
        utility.EventDispatcher.__init__(self)

class NotificationService(service.Service):

    def register_notifier(self, observerfn, *args, **kwargs):
        self.parent.addObserver('//event/pubsub/notify', observerfn,
                                *args, **kwargs)
