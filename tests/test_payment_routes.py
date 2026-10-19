import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import Message, Update
from aiogram.methods import SendMessage, SendInvoice, AnswerPreCheckoutQuery, RefundStarPayment
from playerok_bot.telegram.bot import create_router
from playerok_bot.telegram.billing import Billing
from playerok_bot.telegram.config import TelegramSettings
from playerok_bot.telegram.profiles import Profiles
from playerok_bot.telegram.workers import Workers


class Session(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
    async def close(self):
        pass
    async def stream_content(self, *args, **kwargs):
        yield b''
    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (AnswerPreCheckoutQuery,RefundStarPayment)):
            return True
        if isinstance(method,(SendMessage,SendInvoice)):
            return Message.model_validate({'message_id':1,'date':1,'chat':{'id':method.chat_id,'type':'private'},'text':getattr(method,'text','invoice')})
        raise AssertionError(type(method))


def test_real_router_paywall_checkout_receipt_and_admin_refund(tmp_path):
    async def scenario():
        profiles = Profiles(tmp_path)
        billing = Billing(profiles,frozenset({1}))
        workers = Workers(profiles,20,billing)
        workers.stop_all = AsyncMock()
        settings = TelegramSettings('test',tmp_path,admin_ids=frozenset({1}))
        dispatcher = Dispatcher()
        dispatcher.include_router(create_router(profiles,workers,settings,billing))
        session = Session()
        bot = Bot('123456:abcdefghijklmnopqrstuvwxyzABCDEFG',session=session)
        async def send(uid,text=None,**extra):
            msg={'message_id':1,'date':1,'from':{'id':uid,'is_bot':False,'first_name':'User'},'chat':{'id':uid,'type':'private'},**extra}
            if text is not None: msg['text']=text
            await dispatcher.feed_update(bot,Update.model_validate({'update_id':1,'message':msg}))
        try:
            await send(2,'secret:password')
            assert 'подписка' in session.calls[-1].text
            await send(2,'/set_price 1')
            assert billing.plan()['stars'] == 100
            await send(1,'/set_price 150 10')
            assert billing.plan() == {'stars':150,'days':10}
            await send(2,'/subscribe agree')
            invoice=session.calls[-1]
            assert isinstance(invoice,SendInvoice) and invoice.currency=='XTR'
            assert invoice.prices[0].amount == 150
            for uid,accepted in ((3,False),(2,True)):
                await dispatcher.feed_update(bot,Update.model_validate({'update_id':2,'pre_checkout_query':{
                    'id':'checkout','from':{'id':uid,'is_bot':False,'first_name':'User'},
                    'currency':'XTR','total_amount':150,'invoice_payload':invoice.payload}}))
                assert session.calls[-1].ok is accepted
            receipt={'currency':'XTR','total_amount':150,'invoice_payload':invoice.payload,
                     'telegram_payment_charge_id':'real-charge','provider_payment_charge_id':''}
            await send(2,successful_payment=receipt)
            end=billing.until(2)
            await send(2,successful_payment=receipt)
            assert billing.until(2)==end
            await send(2,'secret:password')
            db=profiles.storage(2)
            assert db.counts()['queued']==1
            db.close()
            await send(2,'/refund 1 confirm')
            assert billing.active(2)
            await send(1,'/refund 1 confirm')
            assert not billing.active(2)
            workers.stop_all.assert_awaited_with(2)
            await send(2,'/export')
            assert '/export_confirm' in session.calls[-1].text
        finally:
            profiles.close()
            await dispatcher.storage.close()
            await bot.session.close()
    asyncio.run(scenario())


def test_export_delivery_failure_keeps_stock_and_other_group_is_untouched(tmp_path):
    from aiogram.methods import SendDocument
    from playerok_bot.models import Credential
    from playerok_bot import withdrawal
    class ExportSession(Session):
        fail = True
        async def make_request(self, bot, method, timeout=None):
            if isinstance(method,SendDocument):
                if self.fail:
                    raise TimeoutError()
                self.calls.append(method)
                return Message.model_validate({'message_id':1,'date':1,'chat':{'id':method.chat_id,'type':'private'},
                    'document':{'file_id':'exported-file','file_unique_id':'unique'}})
            return await super().make_request(bot,method,timeout)
    async def scenario():
        profiles=Profiles(tmp_path)
        db=profiles.storage(2)
        db.import_accounts([Credential('first','secret')]); db.close()
        profiles.create_group(2,'Second')
        db=profiles.storage(2)
        db.import_accounts([Credential('second','secret')]); db.close()
        profiles.select_group(2,1)
        billing=Billing(profiles,frozenset({1}))
        workers=Workers(profiles,20,billing)
        async def probe(group,mode):
            assert mode=='withdraw'
            workers.environment(group)
            db=profiles.storage(group.user_id,group.group_id)
            withdrawal.prepare_remote(db,[])
            db.close()
        workers.probe=probe
        session=ExportSession()
        bot=Bot('123456:abcdefghijklmnopqrstuvwxyzABCDEFG',session=session)
        dispatcher=Dispatcher()
        dispatcher.include_router(create_router(profiles,workers,TelegramSettings('test',tmp_path),billing))
        async def send(text):
            await dispatcher.feed_update(bot,Update.model_validate({'update_id':1,'message':{
                'message_id':1,'date':1,'from':{'id':2,'is_bot':False,'first_name':'User'},
                'chat':{'id':2,'type':'private'},'text':text}}))
        try:
            await send('/export_confirm 1')
            db=profiles.storage(2,1)
            assert db.counts()['queued']==1 and withdrawal.state(db)=='ready'
            db.close()
            await send('/run')
            assert not workers.running(2)
            session.fail=False
            await send('/export_confirm 1')
            db=profiles.storage(2,1)
            assert db.counts()['queued']==0 and withdrawal.state(db)=='done'
            db.close()
            db=profiles.storage(2,2)
            assert db.counts()['queued']==1
            db.close()
            await send('/export_file')
            assert session.calls[-1].document=='exported-file'
        finally:
            profiles.close()
            await dispatcher.storage.close()
            await bot.session.close()
    asyncio.run(scenario())
