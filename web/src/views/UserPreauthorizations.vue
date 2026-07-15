<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import ClockInPreauthorizationPage from '../components/ClockInPreauthorizationPage.vue'

const route = useRoute()
const userId = computed(() => Number(route.params.id))
</script>

<template>
  <ClockInPreauthorizationPage mode="admin" :user-id="userId" />
</template>
