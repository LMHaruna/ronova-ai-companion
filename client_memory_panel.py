"""Memory management embedded in the existing light phone settings."""
from datetime import datetime
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QLineEdit, QPlainTextEdit,
    QScrollArea, QVBoxLayout, QWidget)
from client_memory import CATEGORIES, STAGES
from client_relationship import MIN_AFFINITY, MAX_AFFINITY
from client_affinity import AffinityBar
from client_panels import AnimatedStack, SettingsRow, Switch, button, group, navigation, note

SOURCES={'automatic':'自动识别','explicit':'明确记住','manual':'手动编辑','restored':'恢复旧版本'}
REASONS={'baseline':'升级时保留的原记录','manual_add':'手动添加','manual_edit':'手动修改',
    'explicit':'明确要求记住','automatic_add':'自动追加','automatic_update':'明确纠正后更新',
    'protected_add':'追加保存，旧信息保留','restore':'恢复旧版本'}


def plain_note(text):
    label=note(text); label.setTextFormat(Qt.TextFormat.PlainText); return label


def date_text(value): return datetime.fromtimestamp(value).strftime('%Y-%m-%d %H:%M:%S')


class MemoryPane(QWidget):
    def __init__(self, main, back):
        super().__init__(); self.main=main; self.back=back
        self.setObjectName('memoryPage')
        self.pending=None; self.seen_revision=-1; self.edit_id=None
        self.confirm_action='delete'
        self.list_signature=None
        outer=QVBoxLayout(self); outer.setContentsMargins(0,0,0,0); outer.setSpacing(0)
        self.stack=AnimatedStack(); outer.addWidget(self.stack,1)
        self.home=QWidget(); self.editor=QWidget(); self.stages=QWidget(); self.confirmation=QWidget(); self.history=QWidget()
        for page in (self.home,self.editor,self.stages,self.confirmation,self.history): self.stack.addWidget(page)
        self.status_label=note(''); self.status_label.setFixedHeight(30)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter); outer.addWidget(self.status_label)
        layout,body=self.page(self.home,'记忆与关系',back)
        self.enabled=Switch(); self.enabled.setAccessibleName('使用长期记忆')
        self.enabled.toggled.connect(lambda value:self.run(lambda:self.main.controller.configure_memory(enabled=value)))
        self.automatic=Switch(); self.automatic.setAccessibleName('自动识别关键信息')
        self.automatic.toggled.connect(lambda value:self.run(lambda:self.main.controller.configure_memory(auto_memory=value)))
        self.stage_row=SettingsRow('关系阶段',callback=self.choose_stage)
        self.affinity_label=note('好感度 0 / 100')
        # The single meter belongs to the main window, outside all phone pages.
        self.affinity_bar=main.affinity_bar
        affinity=QWidget(); affinity_layout=QVBoxLayout(affinity); affinity_layout.setContentsMargins(14,8,14,12)
        affinity_layout.addWidget(self.affinity_label)
        self.reset_button=button('重置好感度',self.confirm_reset,'secondaryButton')
        body.addWidget(group([SettingsRow('使用长期记忆',toggle=self.enabled),
            SettingsRow('自动记忆',toggle=self.automatic),self.stage_row,affinity]))
        body.addWidget(self.reset_button)
        body.addWidget(note('成功回复后，当前模型会额外进行一次后台文字分析，自动更新记忆与关系。记忆存于本机，对话时提供给模型。'))
        self.analysis_label=note(''); self.analysis_label.setFixedHeight(32); body.addWidget(self.analysis_label)
        add=button('添加记忆',lambda:self.edit(None),'primaryButton'); body.addWidget(add)
        self.count_label=note(''); body.addWidget(self.count_label)
        self.list_body=QWidget(); self.list_layout=QVBoxLayout(self.list_body)
        self.list_layout.setContentsMargins(0,0,0,0); self.list_layout.setSpacing(10)
        body.addWidget(self.list_body)
        self.clear_button=button('清除全部记忆',lambda:self.confirm_delete(None),'secondaryButton')
        body.addWidget(self.clear_button); body.addStretch(1)

        layout,body=self.page(self.editor,'记忆',self.show_home)
        body.addWidget(note('分类'))
        self.category=QComboBox(); self.category.setAccessibleName('记忆分类')
        for key,label in CATEGORIES.items(): self.category.addItem(label,key)
        self.category.setStyleSheet('QComboBox{background:white;color:#252B35;border:1px solid #E7EAF0;border-radius:10px;padding:9px;} QComboBox QAbstractItemView{background:white;color:#252B35;selection-background-color:#F4C7D2;}')
        body.addWidget(self.category); body.addWidget(note('记忆内容'))
        self.content=QPlainTextEdit(); self.content.setAccessibleName('记忆内容')
        self.content.setPlaceholderText('例如：我喜欢喝热茶，不喜欢太甜。')
        self.content.setFixedHeight(134)
        self.content.setStyleSheet('background:white;color:#252B35;border:1px solid #E7EAF0;border-radius:12px;padding:10px;')
        body.addWidget(self.content)
        self.length_label=note('0 / 300 字'); body.addWidget(self.length_label)
        self.source_label=plain_note(''); body.addWidget(self.source_label)
        self.history_button=button('查看来源与更新历史',self.show_versions,'secondaryButton'); body.addWidget(self.history_button)
        self.content.textChanged.connect(lambda:self.length_label.setText(f'{len(self.content.toPlainText().strip())} / 300 字'))
        self.save_button=button('保存记忆',self.save,'primaryButton'); body.addWidget(self.save_button)
        self.delete_button=button('删除这条记忆',lambda:self.confirm_delete(self.edit_id),'secondaryButton')
        body.addWidget(self.delete_button)
        body.addWidget(note('修改记忆会结束当前通话与回复，并开始新的对话上下文；原聊天文字仍保留。'))
        body.addStretch(1)

        layout,body=self.page(self.stages,'关系阶段',self.show_home)
        self.relationship_label=note(''); body.addWidget(self.relationship_label)
        body.addWidget(note('生气 −20～−1：好感度为负，称呼“凡人”。\n相识 0–29：克制、有距离，称呼以“你”为主。\n熟悉达到 30：更多调侃和具体关心，同样以“你”为主。\n亲密达到 70：明显偏爱，使用专属称呼。'))
        body.addWidget(note('关系自动变化。降档有缓冲，重复内容不加分，退出或沉默不扣分。各阶段共用长期记忆。'))
        body.addWidget(note('羁绊：满100开启誓约，完成后进入；已缔结者达到90保持或恢复，降分保留誓约。'))
        self.oath_hint=plain_note('誓约入口位于主界面立绘右下方。好感度达到100后可开启。'); body.addWidget(self.oath_hint)
        self.oath_reset=button('重置誓约',self.confirm_oath_reset,'secondaryButton'); body.addWidget(self.oath_reset)
        body.addWidget(note('亲密关系下的称呼'))
        self.close_name=QLineEdit(); self.close_name.setObjectName('modelInput'); self.close_name.setMaxLength(20)
        self.close_name.setAccessibleName('亲密关系下的专属称呼'); self.close_name.setMinimumHeight(37)
        body.addWidget(self.close_name)
        self.name_save=button('保存亲密称呼',lambda:self.run(lambda:self.main.controller.configure_memory(close_name=self.close_name.text())),'primaryButton')
        body.addWidget(self.name_save)
        body.addWidget(note('保存称呼会结束当前通话与回复，从新上下文继续。重置好感度保留称呼、记忆和聊天记录。'))
        body.addStretch(1)

        layout,body=self.page(self.confirmation,'确认操作',self.cancel_confirmation)
        self.delete_detail=plain_note(''); body.addStretch(1); body.addWidget(self.delete_detail)
        self.confirm_button=button('确认删除',self.delete_confirmed,'primaryButton'); body.addWidget(self.confirm_button)
        body.addWidget(button('取消',self.cancel_confirmation,'secondaryButton')); body.addStretch(2)

        layout,body=self.page(self.history,'更新历史',self.back_to_editor)
        body.addWidget(note('每个版本显示来源与原话依据。恢复会保留所有历史；删除记忆会同时删除这些历史。升级前未保留的旧内容无法补回。'))
        self.history_body=QWidget(); self.history_layout=QVBoxLayout(self.history_body)
        self.history_layout.setContentsMargins(0,0,0,0); self.history_layout.setSpacing(10)
        body.addWidget(self.history_body)
        self.load_versions_button=button('加载更早的版本',self.load_versions,'secondaryButton'); body.addWidget(self.load_versions_button)
        body.addStretch(1)

    @staticmethod
    def page(widget, title, back):
        layout=QVBoxLayout(widget); layout.setContentsMargins(0,0,0,0); layout.setSpacing(0)
        layout.addWidget(navigation('‹ 返回',title,back))
        scroll=QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        container=QWidget(); body=QVBoxLayout(container)
        body.setContentsMargins(14,10,14,18); body.setSpacing(12)
        scroll.setWidget(container); layout.addWidget(scroll,1)
        return layout,body

    def open(self):
        self.status_label.clear(); self.show_home(); self.refresh(force=True)

    def show_home(self):
        self.stack.setCurrentWidget(self.home)

    def edit(self, record):
        if self.pending: return
        self.edit_id=record['id'] if record else None
        self.edit_updated=record['updated'] if record else None
        self.content.setPlainText(record['content'] if record else '')
        self.category.setCurrentIndex(self.category.findData(record['category'] if record else 'fact'))
        self.delete_button.setVisible(record is not None)
        self.source_label.setText(('来源：'+SOURCES.get(record['source'],record['source'])+' · '+date_text(record['updated'])+
            ('\n原话依据：'+record['evidence'] if record['evidence'] else '')) if record else '')
        self.history_button.setVisible(record is not None)
        self.status_label.clear(); self.stack.setCurrentWidget(self.editor); self.content.setFocus()

    def save(self):
        self.run(lambda:self.main.controller.save_memory(self.content.toPlainText(),self.category.currentData(),
            self.edit_id,expected_updated=self.edit_updated))

    def show_versions(self):
        if self.pending or not self.edit_id: return
        self.history_offset=0
        while self.history_layout.count():
            old=self.history_layout.takeAt(0).widget(); old.hide(); old.deleteLater()
        if self.load_versions():
            self.stack.setCurrentWidget(self.history)
            self.history.findChild(QScrollArea).verticalScrollBar().setValue(0)

    def load_versions(self):
        try: rows=self.main.controller.memory.versions(self.edit_id,limit=20,offset=self.history_offset)
        except ValueError as exc:
            self.status_label.setText(str(exc)); self.show_home(); return False
        for version in rows:
            card=QWidget(); card.setObjectName('groupCard'); layout=QVBoxLayout(card)
            layout.setContentsMargins(12,10,12,10); layout.setSpacing(8)
            layout.addWidget(plain_note(date_text(version['created'])+' · '+REASONS.get(version['reason'],version['reason'])+
                (' · 当前版本' if version['is_current'] else '')))
            layout.addWidget(plain_note(CATEGORIES[version['category']]+' · 来源：'+SOURCES.get(version['source'],version['source'])))
            layout.addWidget(plain_note(version['content']))
            if version['evidence']: layout.addWidget(plain_note('原话依据：'+version['evidence']))
            restore=button('当前版本' if version['is_current'] else '恢复此版本',lambda _checked=False,value=version:self.confirm_restore(value),'secondaryButton')
            restore.setEnabled(not version['is_current']); layout.addWidget(restore)
            self.history_layout.addWidget(card)
        self.history_offset+=len(rows); self.load_versions_button.setVisible(len(rows)==20)
        return True

    def back_to_editor(self):
        record=next((row for row in self.main.controller.memory.snapshot()['memories'] if row['id']==self.edit_id),None)
        if record: self.edit(record)
        else: self.show_home()

    def cancel_confirmation(self):
        self.stack.setCurrentWidget(self.history) if self.confirm_action=='restore' else self.show_home()

    def confirm_restore(self, version):
        if self.pending: return
        current=next((row for row in self.main.controller.memory.snapshot()['memories'] if row['id']==self.edit_id),None)
        if not current: self.status_label.setText('这条记忆已不存在'); self.show_home(); return
        self.restore_id=version['id']; self.restore_updated=current['updated']; self.confirm_action='restore'
        self.delete_detail.setText('恢复这个版本？\n'+version['content']+'\n当前内容与旧版本都会保留在更新历史里。当前通话与回复会结束，之后使用恢复的记忆。')
        self.confirm_button.setText('确认恢复'); self.stack.setCurrentWidget(self.confirmation)

    def choose_stage(self):
        if self.pending: return
        self.refresh(force=True)
        self.close_name.setText(self.main.controller.memory.profile()['close_name'])
        self.stack.setCurrentWidget(self.stages)

    def confirm_oath_reset(self):
        if self.pending: return
        self.confirm_action='oath_reset'
        self.delete_detail.setText('重置誓约？\n清除缔结状态与日期，保留好感度、普通记忆和聊天记录。当前通话与回复会结束，两种后端从新的上下文继续，旧聊天不会恢复誓约。')
        self.confirm_button.setText('重置誓约'); self.stack.setCurrentWidget(self.confirmation)

    def confirm_reset(self):
        if self.pending: return
        self.confirm_action='reset'
        self.delete_detail.setText('重置好感度？\n好感度归零，回到相识。誓约记录、长期记忆、聊天记录和亲密称呼全部保留，当前通话可以继续。')
        self.confirm_button.setText('重置好感度'); self.stack.setCurrentWidget(self.confirmation)

    def confirm_delete(self, identifier):
        if self.pending: return
        self.delete_id=identifier
        self.confirm_action='delete'; self.confirm_button.setText('确认删除')
        self.delete_detail.setText('删除这条记忆？' if identifier else '清除全部长期记忆？好感度与关系状态仍保留。')
        self.delete_detail.setText(self.delete_detail.text()+'\n当前通话与回复会结束，后续对话使用新的上下文，聊天文字仍保留。')
        self.stack.setCurrentWidget(self.confirmation)

    def delete_confirmed(self):
        if self.confirm_action=='reset': self.run(self.main.controller.reset_relationship)
        elif self.confirm_action=='oath_reset': self.run(self.main.controller.reset_oath)
        elif self.confirm_action=='restore': self.run(lambda:self.main.controller.restore_memory(
            self.edit_id,self.restore_id,expected_updated=self.restore_updated))
        else: self.run(lambda:self.main.controller.delete_memory(self.delete_id) if self.delete_id else self.main.controller.clear_memories())

    def controls(self):
        return (self.save_button,self.confirm_button,self.enabled,self.automatic,self.name_save,self.reset_button,self.oath_reset,self.history_button,self.load_versions_button)

    def run(self, operation):
        if self.pending: return
        try:
            self.pending=operation(); self.status_label.setText('正在保存…')
            for control in self.controls(): control.setEnabled(False)
        except (ValueError,RuntimeError) as exc:
            self.status_label.setText(str(exc)); self.refresh(force=True)

    def refresh(self, force=False):
        if self.pending and self.pending.done():
            success=self.pending.result(); self.pending=None
            for control in self.controls(): control.setEnabled(True)
            self.status_label.setText('已保存' if success else self.main.controller.snapshot()['message'] or '未完成，请重试')
            if success: self.show_home()
            force=True
        store=self.main.controller.memory
        if self.isVisible():
            analysis=self.main.controller.companion.snapshot()
            message=analysis['message']
            if self.analysis_label.text()!=message: self.analysis_label.setText(message)
        if self.isVisible() or force:
            profile=store.profile(); available=self.main.controller.oath_available()
            self.oath_reset.setVisible(profile['oath_completed'])
            self.oath_reset.setEnabled(not self.pending and available)
            self.oath_hint.setText(('缔结于 '+date_text(profile['oath_completed_at']) if profile['oath_completed'] else
                '好感度达到100后可开启' if profile['affinity']<100 else '好感度已满，可以缔结誓约')+
                '\n誓约入口位于主界面立绘右下方。'+
                ('\n请结束当前通话、回复、播放或保存操作后再开始' if not available else ''))
        if not force and (not self.isVisible() or self.seen_revision==store.revision): return
        snapshot=store.snapshot(); self.seen_revision=snapshot['revision']
        self.enabled.sync(snapshot['enabled']); self.automatic.sync(snapshot['auto_memory'])
        self.stage_row.value.setText(STAGES[snapshot['stage']])
        self.affinity_label.setText(f"好感度 {snapshot['affinity']} / 100"+(' · 生气中' if snapshot['angry'] else ''))
        self.affinity_bar.setValue(snapshot['affinity'])
        self.relationship_label.setText('当前：'+STAGES[snapshot['stage']]+' · 称呼：'+snapshot['appellation'])
        records=snapshot['memories']
        signature=tuple((row['id'],row['updated']) for row in records)
        if self.list_signature==signature: return
        self.list_signature=signature
        while self.list_layout.count():
            old=self.list_layout.takeAt(0).widget(); old.hide(); old.deleteLater()
        self.count_label.setText(f'已保存 {len(records)} 条记忆')
        self.clear_button.setEnabled(bool(records))
        if not records: self.list_layout.addWidget(note('还没有记忆。对话时会自动识别身份、偏好、边界和重要事情，也可以手动添加。'))
        for record in records:
            card=QWidget(); card.setObjectName('groupCard'); layout=QVBoxLayout(card)
            layout.setContentsMargins(12,8,12,8); layout.setSpacing(4)
            layout.addWidget(note(CATEGORIES[record['category']]+(' · 自动记忆' if record['source']=='automatic' else '')))
            preview=' '.join(record['content'].split()); preview=preview[:60]+'…' if len(preview)>60 else preview
            row=SettingsRow(preview,callback=lambda value=record:self.edit(value))
            if len(preview)>28: row.setMinimumHeight(80)
            row.title.setTextFormat(Qt.TextFormat.PlainText); row.title.setWordWrap(True)
            layout.addWidget(row); self.list_layout.addWidget(card)
