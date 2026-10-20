from aiogram import BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, BufferedInputFile
from ..errors import BotError, ConfigurationError
from .. import withdrawal


class ExportAccess(BaseMiddleware):
    ALLOWED = {'start','help','commands','export','export_confirm','export_cancel','export_file',
        'subscription','subscribe','payments','terms','paysupport','support','admin','admin_payments','refund','set_price',
        'stats','status','daily','allstats','groups','group','stop','stop_all','settings','bump_status','fp_errors','cancel'}

    def __init__(self, profiles):
        self.profiles = profiles

    async def __call__(self, handler, event, data):
        words = (event.text or '').split(maxsplit=1)
        command = words[0].split('@')[0][1:] if words and words[0].startswith('/') else ''
        if not (event.successful_payment or event.refunded_payment) and command not in self.ALLOWED:
            storage = self.profiles.storage(event.from_user.id)
            try:
                withdrawal.assert_idle(storage)
            except BotError as exc:
                await event.answer(str(exc))
                return
            finally:
                storage.close()
        return await handler(event, data)


def register_export(router, profiles, workers):
    @router.message(Command('export'))
    async def export(message: Message):
        group = profiles.get(message.from_user.id)
        await message.answer(f'📤 Забрать аккаунты\nГруппа «{group.group_name}» · №{group.group_id}\n\nПеред выгрузкой\n'
            '1. Выполните /stop и вручную снимите её лоты с продажи на обеих площадках. Не удаляйте ссылки из настроек.\n'
            '2. Завершите текущие сделки и сверку карантина.\n'
            '3. Не меняйте лоты на сайтах во время выгрузки.\n'
            '\nЧто произойдёт\nБот очистит автовыдачу неактивных лотов FunPay, пришлёт .txt и удалит выданные логины и пароли из рабочего склада. '
            'Ссылки этой группы будут убраны из настроек; объявления Playerok не удаляются. Данные в старых резервных копиях не стираются.\n'
            f'\nПодтвердить: /export_confirm {group.group_id}\nПодробнее: /help export')

    @router.message(Command('export_file'))
    async def export_file(message: Message):
        storage = profiles.storage(message.from_user.id)
        try:
            row = storage._connection.execute("SELECT value FROM bot_state WHERE key='export_file'").fetchone()
        finally:
            storage.close()
        if row and row[0]:
            await message.answer_document(row[0], caption='Последняя выгрузка выбранной группы.')
        else:
            await message.answer('Сохранённой выгрузки нет.')

    @router.message(Command('export_cancel'))
    async def cancel(message: Message):
        storage = profiles.storage(message.from_user.id)
        try:
            if withdrawal.state(storage) in {'ready','delivered'}:
                raise ConfigurationError('Файл уже подготовлен. Завершите выгрузку: /export_confirm ID_ГРУППЫ.')
            from ..inventory import Inventory
            if any(op['kind'] == 'withdraw' for op in Inventory(storage).pending_edits()):
                raise ConfigurationError('Результат снятия FunPay ещё неизвестен. Повторите /export_confirm для сверки.')
            storage.set_state_text('export_state', '')
            await message.answer('Выгрузка отменена. Обслуживание остаётся остановленным; проверьте лоты перед /run.')
        except BotError as exc:
            await message.answer(str(exc))
        finally:
            storage.close()

    @router.message(Command('export_confirm'))
    async def confirm(message: Message, command: CommandObject):
        group = profiles.get(message.from_user.id)
        if (command.args or '').strip() != str(group.group_id):
            await message.answer('Укажите номер выбранной группы. Инструкция: /export.')
            return
        await workers.stop(group.user_id, group.group_id)
        storage = profiles.storage(group.user_id, group.group_id)
        try:
            if withdrawal.state(storage) not in {'ready','delivered'}:
                storage.set_state_text('export_state','preparing')
                await message.answer('Проверяю снятие лотов с продажи и готовлю выгрузку…')
                await workers.probe(group, 'withdraw')
            if withdrawal.state(storage) == 'ready':
                withdrawal.verify_source_file(storage, profiles.user_dir(group.user_id, group.group_id) / 'accaunts.txt')
                content, count = withdrawal.payload(storage)
                if count:
                    sent = await message.answer_document(BufferedInputFile(content, filename=f'accounts-group-{group.group_id}.txt'),
                        caption=f'Непроданные аккаунты: {count}. Сохраните файл.')
                    if not sent.document:
                        raise ConfigurationError('Telegram не подтвердил отправку файла. Повторите команду.')
                    file_id = sent.document.file_id
                else:
                    file_id = ''
                withdrawal.finish(storage, file_id)
            if withdrawal.state(storage) != 'delivered':
                raise ConfigurationError('Отправка склада не подтверждена. Повторите выгрузку; данные сохранены.')
            # Keep the group locked if either file cleanup or configuration write fails.
            source = profiles.user_dir(group.user_id, group.group_id) / 'accaunts.txt'
            # On retry after delivery, keep any newly added file data rather than erase it.
            cleared = '# Склад выгружен через Telegram\n'
            if not source.exists() or source.read_text(encoding='utf-8') != cleared:
                withdrawal.verify_source_file(storage, source)
                source.write_text(cleared, encoding='utf-8')
            config = {**profiles.get(group.user_id, group.group_id).config, 'items': [], 'funpay_items': []}
            with profiles.connection:
                profiles.connection.execute('UPDATE product_groups SET config=?,enabled=0 WHERE user_id=? AND group_id=?',
                    (profiles._encode(config), group.user_id, group.group_id))
            storage.set_state_text('export_state', 'done')
            await message.answer('Выгрузка завершена. Выданные аккаунты удалены со склада, лоты отвязаны от группы. Последний файл: /export_file.')
        except BotError as exc:
            await message.answer(str(exc) + '\nГруппа остановлена. Повторить: /export_confirm ' + str(group.group_id) + '. Отмена до подготовки файла: /export_cancel.')
        except Exception as exc:
            await message.answer('Выгрузка не завершена (' + type(exc).__name__ + '). Состояние сохранено; повторите /export_confirm ' + str(group.group_id) + '.')
        finally:
            storage.close()
