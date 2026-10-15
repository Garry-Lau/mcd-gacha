"""mcd-gacha 服务层。

模块职责：
- mcp_client  MCP 调用的唯一出口（硬性约束 #2）
- nutrition   营养数据 + 菜单价格归一化为 FoodItem
- coupon      优惠券归一化为 Coupon
- health      智能手表热量消耗 Provider（可插拔）
- profile     用户画像：BMR / TDEE / 目标热量
- recommender 契合度算法与组合技推荐
"""

__all__ = [
    "mcp_client",
    "nutrition",
    "coupon",
    "health",
    "profile",
    "recommender",
]
