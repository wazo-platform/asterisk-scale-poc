import aiohttp
from aiohttp import web
import asyncio
import asynqp
import logging
import json
import yaml
import consul.aio
import os
import uvicorn
from fastapi import FastAPI

RECONNECT_RATE = 1

logger = logging.getLogger(__name__)


class Context:

    def __init__(self):
        self.host = os.environ.get('APP_HOST', '127.0.0.1')
        self.port = int(os.environ.get('APP_PORT', '8000'))

        self.api_endpoint = os.environ.get('API_ENDPOINT',
                                           'http://localhost:8088')
        self.api_username = os.environ.get('API_USERNAME', 'wazo')
        self.api_password = os.environ.get('API_PASSWORD', 'wazo')

        self.amqp_host = os.environ.get('AMQP_HOST', '127.0.0.1')
        self.amqp_port = int(os.environ.get('AMQP_PORT', '5672'))
        self.amqp_username = os.environ.get('AMQP_USERNAME', 'guest')
        self.amqp_password = os.environ.get('AMQP_PASSWORD', 'guest')
        self.amqp_exchange = os.environ.get('AMQP_EXCHANGE', 'wazo')

        self.consul_host = os.environ.get('CONSUL_HOST', '127.0.0.1')
        self.consul_port = int(os.environ.get('CONSUL_PORT', '8500'))

    def from_conf(self, conf="app.yml"):
        doc = {}
        if os.path.isfile(conf):
            with open(conf) as f:
                doc = yaml.load(f, Loader=yaml.FullLoader)

        self.host = doc.get('address', self.host)
        self.port = doc.get('port', self.port)

        api = doc.get('api')
        if api:
            self.api_endpoint = api.get('endpoint')
            self.api_username = api.get('username')
            self.api_password = api.get('password')

        amqp = doc.get('amqp')
        if amqp:
            self.amqp_host = amqp.get('host')
            self.amqp_port = amqp.get('port')
            self.amqp_username = amqp.get('username')
            self.amqp_password = amqp.get('password')
            self.amqp_exchange = amqp.get('exchange')

        consul = doc.get('consul')
        if consul:
            self.consul_host = consul.get('host')
            self.consul_port = consul.get('port')


class Channel:

    def __init__(self, obj):
        self.obj = obj

    @property
    def dialplan(self):
        return self.obj.get('dialplan', {})

    @property
    def app_name(self):
        dialplan = self.dialplan
        return dialplan.get('app_data')

    @property
    def id(self):
        return self.obj.get('id')

    @property
    def state(self):
        return self.obj.get('state')


class Consumer:

    def __init__(self, queue):
        self.queue = queue

    def __call__(self, msg):
        self.queue.put_nowait(msg)

    def on_error(self, exc):
        logging.error("Connection lost while consuming queue : %s" % exc)


class Application:

    def __init__(self, context, id, name):
        self.context = context
        self.id = id
        self.name = name

        self.fastapi = FastAPI()

    def launch(self):
        loop = asyncio.get_event_loop()
        queue = asyncio.Queue()

        reconnect_task = loop.create_task(self.reconnector(loop, queue))
        process_msgs_task = loop.create_task(
            self.process_msgs(queue))
        register_task = loop.create_task(self.register_loop(loop))

        api_task = loop.create_task(self.run_api())

        try:
            loop.run_until_complete(api_task)
        finally:
            register_task.cancel()
            reconnect_task.cancel()
            process_msgs_task.cancel()

            loop.run_until_complete(register_task)
            loop.run_until_complete(reconnect_task)
            loop.run_until_complete(process_msgs_task)

            loop.close()

    async def run_api(self):
        try:
            self.fastapi.get("/status")(self.status)

            config = uvicorn.Config(self.fastapi,
                                    host="0.0.0.0", port=self.context.port)

            server = uvicorn.Server(config)

            await server.serve()
        except asyncio.CancelledError:
            pass

    async def status(self):
        return {'state': 'ok'}

    async def connect_and_consume(self, queue):
        connection = await asynqp.connect(
            self.context.amqp_host, self.context.amqp_port,
            username=self.context.amqp_username,
            password=self.context.amqp_password)
        try:
            channel = await connection.open_channel()
            exchange = await channel.declare_exchange(
                self.context.amqp_exchange, 'topic')

            amqp_queue = await channel.declare_queue()
            await amqp_queue.bind(exchange, '#')

            consumer = Consumer(queue)
            await amqp_queue.consume(consumer)

        except asynqp.AMQPError as err:
            logging.error("Could not consume on queue %s" % err)
            await connection.close()
            return None
        return connection

    async def reconnector(self, loop, queue):
        try:
            connection = None
            while True:
                if connection is None or connection.is_closed():
                    logging.info("Connecting to rabbitmq...")
                    try:
                        connection = await self.connect_and_consume(queue)
                    except (ConnectionError, OSError):
                        logging.error("Failed to connect to rabbitmq server. "
                                      "Will retry in {} seconds".format(RECONNECT_RATE))
                        connection = None
                    if connection is None:
                        await asyncio.sleep(RECONNECT_RATE)
                    else:
                        logging.info("Successfully connected and consuming")
                        await self.register_consul(loop)

                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            if connection is not None:
                await connection.close()

    async def process_msgs(self, queue):

        type_state_cb = {
            'StasisStart/Ring': self.onStart,
            'ChannelStateChange/Up': self.onUp,
            'StasisEnd/Up': self.onEnd
        }

        try:
            while True:
                msg = await queue.get()

                obj = json.loads(msg.body)

                asterisk_id = obj.get('asterisk_id', '')

                typ = obj.get('type', '')
                channel = Channel(obj.get('channel', {}))

                key = "%s/%s" % (typ, channel.state)

                callback = type_state_cb.get(key)
                if callback:
                    # NOTE will be handled by a higher level component
                    if channel.app_name == self.name:
                        await callback(asterisk_id, channel)

                msg.ack()
        except asyncio.CancelledError:
            pass

    async def request(self, asterisk_id, path):
        headers = {'X-Asterisk-ID': asterisk_id}
        url = "%s%s" % (self.context.api_endpoint, path)

        logging.debug("Sending request to %s : %s" % (asterisk_id, url))

        return await self.post(url, headers=headers)

    async def post(self, url, headers={}):
        auth = aiohttp.BasicAuth(
            login=self.context.api_username,
            password=self.context.api_password)
        async with aiohttp.ClientSession(auth=auth) as session:
            async with session.post(url, headers=headers) as response:
                return response

    async def register_loop(self, loop):
        try:
            c = consul.aio.Consul(
                host=self.context.consul_host,
                port=self.context.consul_port, loop=loop)

            while True:
                eids = []

                (_, nodes) = await c.health.service("asterisk")
                for node in nodes:
                    service = node.get("Service", {})
                    meta = service.get("Meta", {})
                    eid = meta.get("eid")

                    if eid:
                        eids.append(eid)

                for eid in eids:
                    await self.register_ari(eid)

                await asyncio.sleep(5)
        except asyncio.CancelledError:
            pass

    async def register_consul(self, loop):
        c = consul.aio.Consul(
            host=self.context.consul_host,
            port=self.context.consul_port, loop=loop)
        while True:
            logging.info(
                "Registering application %s in Consul" % self.name)
            try:
                response = await c.kv.put("applications/%s" % self.name, "UP")
                if response is not True:
                    raise Exception("error",
                                    "registering application %s" % self.name)

                response = await c.agent.service.register(
                    self.name, service_id=self.id,
                    address=self.context.host, port=self.context.port,
                )
                if response is not True:
                    raise Exception("error",
                                    "registering service %s" % self.name)

                status_url = "http://%s:%d/status" % (
                    self.context.host, self.context.port)
                response = await c.agent.check.register(
                    self.name, consul.Check.http(status_url, '5s'),
                    service_id=self.id)
                if response is not True:
                    raise Exception("error",
                                    "registering check %s" % self.name)

                logging.info(
                    "Service check %s registered in Consul" % self.name)
                # TODO waiting for a new version of python consul to
                # get the coroutine then return await c.close()
                c.close()

                return
            except Exception as e:
                logging.error("Consul error: %s", e)

            await asyncio.sleep(5)

    async def register_ari(self, asterisk_id):
        while True:
            # NOTE this will change in order to make a call toward consul
            response = None
            if asterisk_id:
                logging.info(
                    "Registering application %s on %s" %
                    (self.name, asterisk_id))
                response = await self.request(
                    asterisk_id, "/ari/amqp/%s" % self.name)
            else:
                logging.info("Registering application %s" % self.name)
                response = await self.post(
                    "%s/ari/amqp/%s" % (self.context.api_endpoint, self.name))

            if response.status <= 299:
                logging.info("Application %s registered" % self.name)
                return
            else:
                logging.error("Error while registering application %s : %s" % (
                    self.name, response.reason))

            await asyncio.sleep(5)

    async def answer(self, asterisk_id, channel):
        logging.info("Answering call on channel : %s" % channel.id)
        response = await self.request(
            asterisk_id, "/ari/channels/%s/answer" % channel.id)
        if response.status <= 299:
            logging.info("Answered channel %s successful" % channel.id)
        else:
            logging.error("Error while answering channel %s : %s" % (
                channel.id, response.reason))

    def run(self):
        pass

    async def onStart(self, asterisk_id, channel):
        pass

    async def onEnd(self, asterisk_id, channel):
        pass

    async def onUp(self, asterisk_id, channel):
        pass