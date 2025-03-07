import logging
import sys
import json
import aiohttp
import ffmpeg
import asyncio
import argparse
import secrets
import socketio
from time import sleep
from typing import Optional, Dict, Awaitable, Any, TypeVar
from asyncio.futures import Future
from pymediasoup import Device
from pymediasoup import AiortcHandler
from pymediasoup.transport import Transport
from pymediasoup.consumer import Consumer
from pymediasoup.producer import Producer
from pymediasoup.sctp_parameters import SctpStreamParameters
# Import aiortc
from aiortc import VideoStreamTrack, RTCIceServer
from aiortc.mediastreams import AudioStreamTrack
from aiortc.contrib.media import MediaPlayer, MediaBlackhole, MediaRecorder
from random import random

ice_server = None
iceServer = None
logging.basicConfig(level=logging.DEBUG)
T = TypeVar("T")

def static_response(c):
    return {
                "rtpCapabilities": {
                    "codecs": c['codecs'],
                    "headerExtensions": c['headerExtensions']
                },
                "sctpCapabilities": {
                    "numStreams": {"OS": 1024, "MIS": 1024}
                }
            }

class Demo:
    def __init__(self, uri, player=None, recorder=MediaBlackhole(), loop=None):
        if not loop:
            if sys.version_info.major == 3 and sys.version_info.minor == 6:
                loop = asyncio.get_event_loop()
            else:
                loop = asyncio.get_running_loop()
        self._loop = loop
        self._uri = uri
        self._player = player
        self._recorder = recorder
        # Save answers temporarily
        self._answers: Dict[str, Future] = {}
        self._websocket = socketio.AsyncClient(logger=True, engineio_logger=True)
        self._device = None

        self._tracks = []

        if player and player.audio:
            audioTrack = player.audio
        else:
            audioTrack = AudioStreamTrack()
        if player and player.video:
            videoTrack = player.video
        else:
            videoTrack = VideoStreamTrack()

        self._videoTrack = videoTrack
        self._audioTrack = audioTrack

        self._tracks.append(videoTrack)
        self._tracks.append(audioTrack)

        self._sendTransport: Optional[Transport] = None
        self._recvTransport: Optional[Transport] = None

        self._producers = []
        self._consumers = []
        self._tasks = []
        self._closed = False
        self.getRouterRtpCapabilities = False

    # websocket receive task
    async def recv_msg_task(self):
        while True:
            await asyncio.sleep(0.5)

    # wait for answer ready
    async def _wait_for(
        self, fut: Awaitable[T], timeout: Optional[float], **kwargs: Any
    ) -> T:
        try:
            return await asyncio.wait_for(fut, timeout=timeout, **kwargs)
        except asyncio.TimeoutError:
            raise Exception("Operation timed out")

    async def createWebRtcTransportCallback(self, e, ans, *callback):
        print('createWebRtcTransportCallback')
        print(e, ans, *callback)
        self._answers['createWebRtcTransport'].set_result(ans)
    async def createWebRtcTransportRecCallback(self, e, ans, *callback):
        print('createWebRtcTransportRecCallback')
        print(e, ans, *callback)

        self._answers['createWebRtcTransportRec'].set_result(ans)

    async def createWebRtcTransport(self):
        print("createWebRtcTransport()")
        response = await self._websocket.emit("request", {'method': 'createWebRtcTransport', "data": {
            "forceTcp": False,
            "producing": True,
            "consuming": False,
            "sctpCapabilities": {
                "numStreams": {
                    "OS": 1024,
                    "MIS": 1024
                }
            }
        }}, None, self.createWebRtcTransportCallback)
    async def createWebRtcTransportRec(self):
        print("createWebRtcTransportRec()")

        response = await self._websocket.emit("request", {'method': 'createWebRtcTransport', "data": {
            "forceTcp": False,
            "producing": False,
            "consuming": True,
            "sctpCapabilities": {
                "numStreams": {
                    "OS": 1024,
                    "MIS": 1024
                }
            }
        }}, None, self.createWebRtcTransportRecCallback)

    async def _send_custom_request(self, request):
        print('_send_custom_request')
        if (request == 'createWebRtcTransport'):
            await self.createWebRtcTransport()
        if (request == 'createWebRtcTransportRec'):
            await self.createWebRtcTransportRec()

        self._answers[request] = self._loop.create_future()

    async def hello(self, e=None, data2=None):
        print('hello')
        print(e, data2)
    async def hello2(self, e=None, data2=None):
        print('hello2')
        print(e, data2)
        self._answers['produce'].set_result(data2)

    async def hello3(self, e=None, data2=None):
        print('hello3')
        print(e, data2)
        self._answers['produceData'].set_result(data2)

    async def _send_request(self, request):
        print('_send_request', request['method'],request)


        if request['method'] == 'produce':
            self._answers['produce'] = self._loop.create_future()
            await self._websocket.emit('request',{'method': request["method"],'data':request["data"]}, None, self.hello2)
        elif request['method'] == 'produceData':
            self._answers['produceData'] = self._loop.create_future()
            await self._websocket.emit('request',{'method': request["method"],'data':request["data"]}, None, self.hello3)
        else:
            self._answers[request["id"]] = self._loop.create_future()
            await self._websocket.emit('request', {'method': request["method"], 'data': request["data"]}, None, self.hello)

    # Generates a random positive integer.
    def generateRandomNumber(self) -> int:
        return round(random() * 10000000)

    async def notificationHandler(self,event,data):
        print('event',event,'data', data)

        # Send a message to the server after connecting
        if event.get('method') == 'roomReady':
            message = {'method': 'join', 'data': {'displayName': 'python'}}  # ,'picture':'https://ca.slack-edge.com/T0LUT5Q9W-U06CSP8L1NU-2fe3e7badac7-512'
            # print('sending join')
            await self._websocket.emit("notification", message)
        elif event.get('method') == 'newConsumer':
            # print('Start cONSUMING',event.get('data').get('rtpParameters'))
            await self.consume(
                id=event.get('data').get('id'),
                producerId=event.get('data').get('producerId'),
                kind=event.get('data').get('kind'),
                rtpParameters=event.get('data').get('rtpParameters'),
                appData=event.get('data').get('appData')
            )
            await self._websocket.emit('notification', {'method': 'resumeConsumer', 'data': {'consumerId': event.get('data').get('id') } }, None, self.hello)

        elif event.get('method') == 'consumerClosed':
            print('calling end')
            await self.close()
            print('await self.close()')
            # self._loop.close()
            #await self._recorder.stop()

            self._closed=True


    async def requestHandler(self,event,data = None):
        print(f"Received event: {event}")

        if event.get('method') == 'mediaConfiguration' and not self.getRouterRtpCapabilities:
            print('device.load')
            self.getRouterRtpCapabilities = True
            self._answers['getRouterRtpCapabilities'].set_result(event.get('data').get('routerRtpCapabilities'))
            global ice_server

            ice_server = event.get('data').get('iceServers')
            await self._device.load(event.get('data').get('routerRtpCapabilities'))
            print("roomReady detected, sending response...")
            a = self._device.rtpCapabilities.dict(exclude_none=True)
            return None, static_response(a)
        else:
            print(f"Unhandled event: {event}, args: {args}")
    async def run(self):
        await self._websocket.connect(uri)
        self._websocket.on('notification', self.notificationHandler)
        self._websocket.on('request', self.requestHandler)
        task_run_recv_msg = asyncio.create_task(self.recv_msg_task())
        self._tasks.append(task_run_recv_msg)

        await self.load()
        await self.createSendTransport()
        await self.createRecvTransport()
        await self.produce()

        await task_run_recv_msg

    async def load(self):
        # Init device
        self._device = Device(
            handlerFactory=AiortcHandler.createFactory(tracks=self._tracks)
        )

        await self._send_custom_request('getRouterRtpCapabilities')
        await asyncio.sleep(0.5)
        ans = await self._wait_for(self._answers['getRouterRtpCapabilities'], timeout=15)

        # Load Router RtpCapabilities
        # await self._device.load(ans["data"])

    async def createSendTransport(self):
        if self._sendTransport is not None:
            return
        # Send create sendTransport request
        await self._send_custom_request('createWebRtcTransport')
        await asyncio.sleep(1)
        ans = await self._wait_for(self._answers['createWebRtcTransport'], timeout=15)

#        print('ice_server',ice_server)
        global iceServer
        if iceServer is None:
            iceServer = RTCIceServer(urls=ice_server[0].get('urls')[0], username=ice_server[0].get('username'),
                     credential=ice_server[0].get('credential'))

        # Create sendTransport
        self._sendTransport = self._device.createSendTransport(
            id=ans["id"],
            iceParameters=ans["iceParameters"],
            iceCandidates=ans["iceCandidates"],
            iceServers=[iceServer],
            dtlsParameters=ans["dtlsParameters"],
            sctpParameters=ans["sctpParameters"],
            appData={'source':'webcam'}

        )

        @self._sendTransport.on("connect")
        async def on_connect(dtlsParameters):
            reqId = self.generateRandomNumber()
            dtls = dtlsParameters.dict(exclude_none=True)
            #dtls['role']='client'
            print(dtls)
            req = {
                "request": True,
                "id": reqId,
                "method": "connectWebRtcTransport",
                "data": {
                    "transportId": self._sendTransport.id,
                    "dtlsParameters": dtls,
                },
            }
            await self._send_request(req)
            await asyncio.sleep(2)
            #ans = await self._wait_for(self._answers[reqId], timeout=15)
            #print(ans)

        @self._sendTransport.on("produce")
        async def on_produce(kind: str, rtpParameters, appData: dict):
            print('@self._sendTransport.on("produce")')
            reqId = self.generateRandomNumber()
            req = {
                "id": reqId,
                "method": "produce",
                "request": True,
                "data": {
                    "transportId": self._sendTransport.id,
                    "kind": kind,
                    "rtpParameters": rtpParameters.dict(exclude_none=True),
                    "appData": appData,
                },
            }
            await self._send_request(req)
            await asyncio.sleep(.1)
            ans = await self._wait_for(self._answers['produce'], timeout=1)
            print('ans = await self._wait_for(self._answers["produce"], timeout=15)')
            print(ans['id'])
            return ans["id"]

        @self._sendTransport.on("producedata")
        async def on_producedata(
            sctpStreamParameters: SctpStreamParameters,
            label: str,
            protocol: str,
            appData: dict,
        ):
            return 100

    async def produce(self):
        if self._sendTransport is None:
            await self.createSendTransport()
        # Join room
        await asyncio.sleep(1)
        # produce
        videoProducer: Producer = await self._sendTransport.produce(
            track=self._videoTrack, stopTracks=False, appData={'source':'webcam'}
        )
        self._producers.append(videoProducer)
        '''audioProducer: Producer = await self._sendTransport.produce(
            track=self._audioTrack, stopTracks=False, appData={'source':'mic', 'kind': 'audio'}
        )
        self._producers.append(audioProducer)'''

        # produce data
        await self.produceData()

    async def produceData(self):
        if self._sendTransport is None:
            print('self._sendTransport is None:')
            await self.createSendTransport()

        '''dataProducer: DataProducer = await self._sendTransport.produceData(
            ordered=False,
            maxPacketLifeTime=5555,
            label="chat",
            protocol="",
            appData={"info": "my-chat-DataProducer"},
        )
        self._producers.append(dataProducer)'''
        while not self._closed:
            await asyncio.sleep(1)
            #dataProducer.send("hello")


    async def createRecvTransport(self):
        if self._recvTransport is not None:
            return
        # Send create recvTransport request
        await self._send_custom_request('createWebRtcTransportRec')
        # await asyncio.sleep(1)
        ans = await self._wait_for(self._answers['createWebRtcTransportRec'], timeout=15)

        # print('ice_server', ice_server)
        # print('ans',ans)
        global iceServer
        if iceServer is None:
            iceServer = RTCIceServer(urls=ice_server[0].get('urls')[0], username=ice_server[0].get('username'),
                                     credential=ice_server[0].get('credential'))
        # Create recvTransport
        self._recvTransport = self._device.createRecvTransport(
            id=ans["id"],
            iceServers=[iceServer],
            iceParameters=ans["iceParameters"],
            iceCandidates=ans["iceCandidates"],
            dtlsParameters=ans["dtlsParameters"],
            sctpParameters=ans["sctpParameters"],
        )

        @self._recvTransport.on("connect")
        async def on_connect(dtlsParameters):
            reqId = self.generateRandomNumber()
            dtls = dtlsParameters.dict(exclude_none=True)
            req = {
                "request": True,
                "id": reqId,
                "method": "connectWebRtcTransport",
                "data": {
                    "transportId": self._recvTransport.id,
                    "dtlsParameters": dtls,
                },
            }
            await self._send_request(req)
            await asyncio.sleep(2)

            #ans = await self._wait_for(self._answers[reqId], timeout=15)
            #print(ans)

    async def consume(self, id, producerId, kind, rtpParameters, appData):
        if self._recvTransport is None:
            print('self._recvTransport is None')
            await self.createRecvTransport()


        consumer: Consumer = await self._recvTransport.consume(
            id=id, producerId=producerId, kind=kind, rtpParameters=rtpParameters, appData=appData
        )
        self._consumers.append(consumer)

        print(recorder)

        self._recorder.addTrack(consumer.track)
        #self._recorder.addTrack(self._videoTrack)
        await asyncio.sleep(2)
        await self._recorder.start()

        print(await consumer.getStats())
        print('consumer.track',consumer.track)
        print(consumer.producerId)
        await asyncio.sleep(1)
        print(await consumer.getStats())
        await asyncio.sleep(1)
        print(await consumer.getStats())
        await asyncio.sleep(1)
        print(await consumer.getStats())
        await asyncio.sleep(1)
        print(await consumer.getStats())
        await asyncio.sleep(1)
        print(consumer.track)


    async def close(self):
        for consumer in self._consumers:
            print('consumer.close()')
            await consumer.close()
        for producer in self._producers:
            print('producer.close()')
            await producer.close()
        for task in self._tasks:
            print('task.cancel()',task)
            task.cancel()
        if self._sendTransport:
            print('await self._sendTransport.close()')
            await self._sendTransport.close()
        if self._recvTransport:
            print('await self._recvTransport.close()')
            await self._recvTransport.close()
        print('await self._recorder.stop()')
        await self._recorder.stop()
        print('await self._recorder.stopped')





if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PyMediaSoup")
    parser.add_argument("room", nargs="?")
    parser.add_argument("--play-from", help="Read the media from a file and sent it.")
    parser.add_argument("--record-to", help="Write received media to a file.")
    args = parser.parse_args()

    if not args.room:
        args.room = secrets.token_urlsafe(8).lower()
    peerId = secrets.token_urlsafe(8).lower()

    uri = "wss://edumeet-v4-demo.geant.org:443/?peerId=python&roomId=asd"

    if args.play_from:
        player = MediaPlayer(args.play_from)
    else:
        player = None

    # create media sink
    if args.record_to:
        recorder = MediaRecorder(args.record_to)
    else:
        recorder = MediaBlackhole()

    # run event loop
    loop = asyncio.get_event_loop()
    def should_end():
        demo.close()
    try:
        demo = Demo(uri=uri, player=player, recorder=recorder, loop=loop)
        loop.run_until_complete(demo.run())
    except KeyboardInterrupt:
        print('Ctrl + C')
        loop.run_until_complete(demo.close())
    finally:
        loop.run_until_complete(demo.close())
    print('ended')