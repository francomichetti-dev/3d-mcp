"""adsk.fusion stand-ins."""


class Design:
    """Only `cast` matters to the bridge: it uses it to decide whether the
    active product is a Design, and returns None when it is not."""

    @staticmethod
    def cast(product):
        if product is None:
            return None
        return product if getattr(product, "_is_design", True) else None


class FeatureOperations:
    NewBodyFeatureOperation = 0
    CutFeatureOperation = 1
    IntersectFeatureOperation = 2
    JoinFeatureOperation = 3


class DesignTypes:
    ParametricDesignType = 0
    DirectDesignType = 1
