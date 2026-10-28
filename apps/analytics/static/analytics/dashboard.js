(function () {
    "use strict";

    var payload = document.getElementById("bi-charts");
    if (!payload || typeof Chart === "undefined") {
        return;
    }

    var charts = JSON.parse(payload.textContent);
    var styles = getComputedStyle(document.documentElement);
    var ink = styles.getPropertyValue("--adm-ink-muted").trim() || "#64748b";
    var grid = styles.getPropertyValue("--adm-border").trim() || "#e2e8f0";

    function format(value, kind) {
        if (value === null || value === undefined) {
            return "—";
        }
        if (kind === "money") {
            return "£" + Number(value).toFixed(2);
        }
        if (kind === "percent") {
            return Number(value).toFixed(1) + "%";
        }
        return value;
    }

    function ticks(kind) {
        return {
            color: ink,
            precision: kind === "integer" ? 0 : undefined,
            callback: function (value) {
                return format(value, kind);
            }
        };
    }

    function valueAxis(spec, extra) {
        return Object.assign(
            {
                beginAtZero: spec.begin_at_zero !== false,
                max: spec.max_value,
                stacked: Boolean(spec.stacked),
                grid: { color: grid },
                ticks: ticks(spec.value_format)
            },
            extra || {}
        );
    }

    function categoryAxis(spec) {
        return {
            stacked: Boolean(spec.stacked),
            grid: { color: grid, display: false },
            ticks: { color: ink, maxRotation: 45, minRotation: spec.horizontal ? 0 : 45 }
        };
    }

    function scalesFor(spec) {
        if (spec.type === "doughnut") {
            return undefined;
        }
        if (spec.dual_axis) {
            return {
                x: categoryAxis(spec),
                money: {
                    position: "left",
                    beginAtZero: true,
                    grid: { color: grid },
                    ticks: ticks("money")
                },
                count: {
                    position: "right",
                    beginAtZero: true,
                    grid: { display: false },
                    ticks: ticks("integer")
                }
            };
        }
        if (spec.horizontal) {
            return { x: valueAxis(spec), y: categoryAxis(spec) };
        }
        return { x: categoryAxis(spec), y: valueAxis(spec) };
    }

    function draw(canvas) {
        var spec = charts[canvas.dataset.chart];
        if (!spec) {
            return;
        }

        new Chart(canvas, {
            type: spec.type,
            data: { labels: spec.labels, datasets: spec.datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                indexAxis: spec.horizontal ? "y" : "x",
                interaction: { intersect: false, mode: "index" },
                scales: scalesFor(spec),
                plugins: {
                    legend: {
                        display: spec.hide_legend !== true,
                        position: "bottom",
                        labels: { color: ink, usePointStyle: false, boxWidth: 28 }
                    },
                    tooltip: {
                        callbacks: {
                            label: function (item) {
                                var value = item.parsed;
                                if (value !== null && typeof value === "object") {
                                    value = spec.horizontal ? value.x : value.y;
                                }
                                var axis = item.dataset.yAxisID;
                                var kind = axis === "money" ? "money" : axis === "count" ? "integer" : spec.value_format;
                                return item.dataset.label + ": " + format(value, kind);
                            }
                        }
                    }
                }
            }
        });
    }

    document.querySelectorAll("canvas[data-chart]").forEach(draw);

    var range = document.querySelector("[data-custom-toggle]");
    if (range) {
        range.addEventListener("change", function () {
            document.querySelectorAll(".bi-field--custom").forEach(function (field) {
                field.hidden = range.value !== "custom";
            });
        });
    }
})();
