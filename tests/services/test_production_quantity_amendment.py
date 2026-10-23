import copy
import pytest
from app.services import one_c_production_order_export as owner
from test_one_c_production_order_export import _mk_run, _mk_item, _mk_mrp_order, _mk_sync_link_for_order


def _native(qty=8):
    return {'Number':'same-number','Date':'2026-10-22T14:00:00','Posted':True,'Комментарий':'unchanged-origin',
            'Продукция':[{'Номенклатура_Key':'item-ref','Количество':qty,'КлючСвязи':'99999','Спецификация_Key':'native-spec'}],
            'Запасы':[{'Количество':qty*2,'КлючСвязи':'stock-key','Номенклатура_Key':'material-ref'}],
            'Операции':[{'КоличествоПлан':8,'НормаВремени':0.078,'Нормочасы':0.624,'КлючСвязи':'operation-key','КлючСвязиПродукция':'99999'}]}


def test_native_reduction_preserves_linked_keys_and_native_specification():
    doc=_native(); original=copy.deepcopy(doc)
    p=owner._reduced_native_payload(doc,5)
    assert doc==original
    assert p['Продукция'][0]=={**doc['Продукция'][0],'Количество':5}
    assert p['Запасы'][0]['Количество']==10
    assert p['Операции'][0]['КоличествоПлан']==5
    assert p['Операции'][0]['Нормочасы']==0.39
    assert p['Операции'][0]['КлючСвязиПродукция']=='99999'


def test_native_already_reduced_does_not_shrink_materials_a_second_time():
    p=owner._reduced_native_payload(_native(5),5)
    assert p['Запасы'][0]['Количество']==10
    assert p['Операции'][0]['КоличествоПлан']==5


@pytest.mark.parametrize('quantity',[0,-1,9,float('nan'),float('inf')])
def test_native_payload_rejects_increases_or_invalid_quantities(quantity):
    with pytest.raises(ValueError):owner._reduced_native_payload(_native(),quantity)


@pytest.mark.parametrize('fail_post',[False,True])
def test_amendment_retry_uses_same_ref_and_does_not_rescale_twice(db_session,monkeypatch,fail_post):
    db=db_session; run=_mk_run(db); item=_mk_item(db,code='amend',ref1c='item-ref')
    order=_mk_mrp_order(db,item,run_id=run.run_id,qty=8)
    link=_mk_sync_link_for_order(db,order);order.order_ref1c=link.target_ref_key;db.commit()
    product=order.products[0]
    monkeypatch.setattr(owner,'_load_odata_config',lambda:{})
    from app.services import production_control_material_availability as materials
    monkeypatch.setattr(materials,'get_materials_snapshot',lambda *a:{'truth_status':'accepted','ledger_generation_id':run.ledger_generation_id,'material_launchable_qty':5})
    class Client:
        def __init__(self):self.doc=_native();self.patches=[];self.calls=[];self.fail=fail_post
        def _make_request(self,*a):return copy.deepcopy(self.doc)
        def patch(self,endpoint,payload):self.patches.append(endpoint);self.doc.update(copy.deepcopy(payload))
        def post_operation(self,endpoint):
            self.calls.append(endpoint)
            if endpoint.endswith('/Unpost'):self.doc['Posted']=False
            else:
                if self.fail:self.fail=False;raise RuntimeError('unknown posting result')
                self.doc['Posted']=True
    client=Client();monkeypatch.setattr(owner,'_create_odata_client',lambda *a:client)
    if fail_post:
        with pytest.raises(RuntimeError):owner.reduce_existing_production_quantity_to_1c(db,product.product_id,expected_quantity=8,quantity=5,dry_run=False)
        assert product.quantity==8
    result=owner.reduce_existing_production_quantity_to_1c(db,product.product_id,expected_quantity=8,quantity=5,dry_run=False)
    assert result['status']=='ok' and product.quantity==5
    assert len(client.patches)==1 and link.target_ref_key in client.patches[0]
    calls_before=len(client.calls)
    again=owner.reduce_existing_production_quantity_to_1c(db,product.product_id,expected_quantity=8,quantity=5,dry_run=False)
    assert again['ref1c']==result['ref1c'] and len(client.patches)==1
    assert client.doc['Запасы'][0]['Количество']==10
    assert len(client.calls)==calls_before
    assert link.payload_hash=='close-payload-hash'  # historical creation receipt remains intact
