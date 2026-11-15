from fastapi import APIRouter

router = APIRouter(prefix="/orders")


@router.get("/{order_id}")
def get_order(order_id: int):
    return {"id": order_id}


@router.post("")
def create_order():
    return {"id": 1}


@router.delete("/{order_id}")
def cancel_order(order_id: int):
    return None
