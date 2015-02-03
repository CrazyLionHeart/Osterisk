#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
Created on 2009-11-15

@author: <a class="linkification-ext"
title="Linkification: mailto:olivier@olihb.com"
href="mailto:olivier@olihb.com">olivier@olihb.com</a>
/**
 *
 * @module osterisk
 * @class osterisk
 */
'''
try:
    import json

    import re
    import logging
    from logging.config import dictConfig
    from os import environ

    from starpy.manager import AMIFactory

    from twisted.internet import defer, reactor
    from twisted.internet import task
    from twisted.enterprise import adbapi

    from stompest.config import StompConfig
    from stompest.sync import Stomp

    from raven import Client
except ImportError as e:
    raise e

current_env = environ.get("APPLICATION_ENV", 'development')

with open('config/%s/config.%s.json' % (current_env, current_env)) as f:
    config = json.load(f)
    dictConfig(config["loggingconfig"])
    dsn = "http://%s:%s@%s" % (config['Raven']['public'],
                               config['Raven']['private'],
                               config['Raven']['host'])


client = Client(dsn)

activemq = config['activemq']
asterisk = config['asterisk']
default_uri = '''failover:(tcp://%(host)s:%(port)d,tcp://%(host)s:%(port)d)?randomize=%(randomize)s,startupMaxReconnectAttempts=%(startupMaxReconnectAttempts)d,initialReconnectDelay=%(initialReconnectDelay)d,maxReconnectDelay=%(maxReconnectDelay)d,maxReconnectAttempts=%(maxReconnectAttempts)d''' % activemq[
    'stomp']
queue = "/topic/%s" % config['queue']['BotNet']


"""
.. py:function:: send_message(sender, recipient, message_body, [priority=1])

   Send a message to a recipient

   :param str sender: The person sending the message
   :param str recipient: The recipient of the message
   :param str message_body: The body of the message
   :param priority: The priority of the message, can be a number 1-5
   :type priority: integer or None
   :return: the message id
   :rtype: int
   :raises ValueError: if the message_body exceeds 160 characters
   :raises TypeError: if the message_body is not a basestring
"""


class Producer(object):

    def __init__(self, config=None):
        if config is None:
            config = StompConfig(default_uri)
        self.config = config

    @property
    def log(self):
        return logging.getLogger('%s.%s' % (__name__, self.__class__.__name__))

    def run(self, messages=None):
        client = Stomp(self.config)
        client.connect()
        j = 0
        for message in messages:
            client.send(queue, message,
                        receipt='message-asterisk-%d' %
                        j, headers={'persistent': 'true'})
            j = j + 1
        client.disconnect(receipt='bye')


def Send_Notify(func_args):
    message = dict(
        body=dict(
            func_name="SwitchBusy",
            func_args=func_args
        ),
        recipient=["*", ],
        profile="system",
        tag=["asterisk", ]
    )
    return json.dumps(message)


def Incoming_call(func_args):
    message = dict(
        body=dict(
            func_name="Incoming_call",
            func_args=func_args
        ),
        recipient=["*", ],
        profile="system",
        tag=["asterisk", ]
    )
    return json.dumps(message)


class callMeFactory(AMIFactory):
    cbconnect = None

    def __init__(self, username, secret):
        AMIFactory.__init__(self, username, secret)

    @property
    def log(self):
        return logging.getLogger('%s.%s' % (__name__, self.__class__.__name__))

    def connect(self):
        df = self.login(asterisk['host'], asterisk['port'])
        if not (self.cbconnect is None):
            df.addCallback(self.cbconnect)
            df.addErrback(self.log.error)

    def clientConnectionLost(self, connector, reason):
        self.log.error("connection lost - connecting again")
        reactor.callLater(1, self.connect)

    def clientConnectionFailed(self, connector, reason):
        self.log.error("connection failed - connecting again")
        reactor.callLater(1, self.connect)


def onDial(protocol, event):

    messages = list()

    logger = logging.getLogger(__name__)

    if 'destination' in event:
        logger.info(event)

        regex_source = re.search("SIP/[0-9]{4}", event['source'])
        regex_dest = re.search("SIP/[0-9]{4}", event['destination'])
        local = list()

        if regex_source:
            source = regex_source.group(0)
            func_args = [source, "busy", ]
            ControlMessage = Send_Notify(func_args)
            messages.append(ControlMessage)
            local.append(source)

        if regex_dest:
            destination = regex_dest.group(0)
            func_args = [destination, "busy"]
            ControlMessage = Send_Notify(func_args)
            messages.append(ControlMessage)
            local.append(destination)

        if len(local) == 2:
            func_args = [source, destination]
            ControlMessage = Incoming_call(func_args)
            messages.append(ControlMessage)
        else:
            logger.info(u"Что-то не срослось")

        if messages:
            Producer().run(messages)


def onHangup(protocol, event):

    messages = list()

    regex = re.search("SIP/[0-9]{4}", event["channel"])

    if regex:
        channel = regex.group(0)
        func_args = [channel, "free"]
        ControlMessage = Send_Notify(func_args)
        messages.append(ControlMessage)

    if messages:
        Producer().run(messages)


def onCdr(protocol, event):
    """
    NOTE: you can return a Deferred here
    """

    logger = logging.getLogger(__name__)

    def finishInsert(dObject, pool):
        logger.info(u'Данные успешно вставлены!')
        logger.info(u"Закрываем подключение к БД")
        pool.close()
        return defer.succeed

    def insertError(dObject, pool):
        logger.error(u'При вставке данных произошла ошибка')
        logger.error(dObject)
        logger.info(u"Закрываем подключение к БД")
        pool.close()
        return defer.fail

    if event['destination'] == 's':
        phone = event['source']
    else:
        phone = event['destination']

    sender = event['channel'] + " " + event['callerid']

    if event['lastapplication'] == 'Queue':
        phone = event["destinationchannel"]

    if event['lastapplication'] == 'BackGround':
        status = "ESCAPED"
    else:
        status = event['disposition']

    messtype = "asterisk"
    message = json.dumps(event)
    dt = event['starttime']
    mess_length = event["billableseconds"]

    logger.info(u"Открываем подключение к БД")

    dbpool = adbapi.ConnectionPool(
        "psycopg2", **config["postgresql"])
    logger.info(u"Кладем данные в базу данных")
    dOperation = dbpool.runOperation("""SELECT *
                                     FROM insert_to_contacts(%s, %s, %s, %s,
                                                             %s, %s, %s, %s,
                                                             %s, %s, %s )
    """, (
        dt, None, phone, sender, messtype, message, mess_length, status,
        None, None, None))

    dOperation.addCallback(finishInsert, dbpool)
    dOperation.addErrback(insertError, dbpool)


def checknetlink(protocol):

    logger = logging.getLogger(__name__)

    def ontimeout():
        logger.info("timeout")
        if dc.active():
            dc.cancel()
        asterisk['timeouttask'].stop()
        protocol.transport.loseConnection()

    def canceltimeout(*val):
        if dc.active():
            dc.cancel()

        logger.info("cancel timeout")
        logger.info(val)

    def success(val):
        pass

    logger.info("setting timeout")
    dc = reactor.callLater(asterisk['timeoutping'], ontimeout)
    df = protocol.ping()
    df.addBoth(canceltimeout)
    df.addCallback(success)
    df.addErrback(ontimeout)


def onLogin(protocol):

    asterisk['timeouttask'] = task.LoopingCall(
        checknetlink, protocol)
    asterisk['timeouttask'].start(asterisk['timeoutloop'])

    protocol.registerEvent("Dial", onDial)
    protocol.registerEvent("Hangup", onHangup)
    protocol.registerEvent("Cdr", onCdr)

    return protocol


def main():
    cm = callMeFactory(
        asterisk['login'], asterisk['secret'])
    cm.cbconnect = onLogin
    cm.connect()


def killapp(*args):
    reactor.stop()
    return True

if __name__ == '__main__':
    reactor.callWhenRunning(main)
    reactor.run()
