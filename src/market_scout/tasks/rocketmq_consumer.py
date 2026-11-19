"""Compatibility shim for the pinned RocketMQ SDK consumer lifecycle."""

from rocketmq.v5.consumer.push import PushConsumer


class ManagedPushConsumer(PushConsumer):
    # SDK 5.1.1 leaves its receive/consumption executors alive and its
    # ACK retry recursion does not test is_running. Keep this narrow
    # compatibility shim with the pinned SDK until upstream fixes it.
    _closing = False

    def _PushConsumer__ack_or_nack(self, *args, **kwargs):
        if not self._closing:
            return super()._PushConsumer__ack_or_nack(*args, **kwargs)

    def _PushConsumer__execute_receive(self, *args, **kwargs):
        if not self._closing:
            return super()._PushConsumer__execute_receive(*args, **kwargs)

    def reset_metric(self, metric):
        # 5.1.1 registers gauges even when the Broker disables metrics,
        # raising on a missing MeterProvider and breaking telemetry.
        if metric and metric.on:
            return super().reset_metric(metric)
        return super(PushConsumer, self).reset_metric(metric)

    def shutdown(self):
        self._closing = True
        try:
            super().shutdown()
        finally:
            consumption = getattr(self, "_PushConsumer__consumption", None)
            executors = [
                getattr(self, "_PushConsumer__receive_message_executor", None),
                getattr(self, "_PushConsumer__ack_or_nack_result_executor", None),
                getattr(consumption, "_Consumption__consumption_executor", None),
            ]
            for executor in executors:
                if executor is not None:
                    executor.shutdown(wait=True, cancel_futures=True)
