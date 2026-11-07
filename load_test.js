import http from 'k6/http';
import { check, sleep } from 'k6';
import { htmlReport } from "https://raw.githubusercontent.com/benc-uk/k6-reporter/main/dist/bundle.js";

export const options = {
    thresholds: {
        http_req_duration: ['p(95)<500'],
        http_req_failed: ['rate<0.01'],
    },

    // 1. BASELINE TEST
    vus: 1, duration: '10s',

    // 2. LOAD TEST

    stages: [
        { duration: '10s', target: 50 },
        { duration: '30s', target: 50 },
        { duration: '10s', target: 0 },
    ],


    // 3. SPIKE TEST

    stages: [
        { duration: '2s', target: 200 },
        { duration: '15s', target: 200 },
        { duration: '5s', target: 0 },
    ],

};

export default function () {
    const BASE_URL = 'http://127.0.0.1:8080';

    const payload = JSON.stringify({
        code: `K6-USER-${__VU}-${__ITER}-${Date.now()}`,
        regular: true
    });
    const params = { headers: { 'Content-Type': 'application/json' } };

    const postRes = http.post(`${BASE_URL}/customers`, payload, params);

    check(postRes, { 'POST status is 201': (r) => r.status === 201 });

    if (postRes.status === 201) {
        const customerId = postRes.json('id');
        const getRes = http.get(`${BASE_URL}/customers/${customerId}`);
        check(getRes, { 'GET status is 200': (r) => r.status === 200 });
    }

    sleep(1);
}

export function handleSummary(data) {
    return {
        "load-report.html": htmlReport(data),
    };
}