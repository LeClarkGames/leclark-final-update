import discord
from discord.ext import commands, tasks
import logging
from cogs.moderation import _mute_member, _issue_warning
import config

log = logging.getLogger(__name__)

class PanelHandlerCog(commands.Cog, name="Panel Handler"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.process_action_queue.start()

    def cog_unload(self):
        self.process_action_queue.cancel()

    @tasks.loop(seconds=2)
    async def process_action_queue(self):
        try:
            task = await self.bot.action_queue.get()
            guild = self.bot.get_guild(task.get('guild_id'))
            moderator = guild.get_member(task.get('moderator_id')) if guild else None
            
            if not guild or not moderator:
                log.error(f"Action queue: Guild or Moderator not found.")
                self.bot.action_queue.task_done()
                return

            action = task.get('action')
            log.info(f"Processing panel action '{action}' for guild {guild.name}")

            # Removed the empty 'elif mod_action == 'warn':' block from here

            if action == 'moderate_user':
                target = guild.get_member(int(task.get('target_id')))
                reason = task.get('reason', 'No reason.') + f" - By {moderator.display_name} via Web Panel"
                mod_action = task.get('mod_action')
                
                if not target:
                    log.warning(f"Could not find member {task.get('target_id')} in guild {guild.id}.")
                else:
                    try:
                        if mod_action == 'ban': await guild.ban(target, reason=reason)
                        elif mod_action == 'kick': await guild.kick(target, reason=reason)
                        # CORRECTED CALL: Pass self.bot as the first argument
                        elif mod_action == 'warn': await _issue_warning(self.bot, target, moderator, reason)
                        elif mod_action == 'timeout':
                            duration = int(task.get('duration', 10))
                            # CORRECTED CALL: Pass None as the first argument (interaction_or_message)
                            await _mute_member(None, target, duration, reason, moderator)
                    except discord.Forbidden:
                        log.error(f"Missing permissions to {mod_action} member {target.id} in guild {guild.id}.")

            elif action == 'run_setup':
                setup_map = {
                    'verification': 'setup_verification',
                    'submission': 'setup_submission_panel',
                    'report': 'setup_report'
                }
                command_name = setup_map.get(task.get('setup_type'))
                if command := self.bot.tree.get_command(command_name):
                    mock_interaction = discord.Interaction(
                        application_id=self.bot.user.id,
                        type=discord.InteractionType.application_command,
                        data={"id": "mock_id", "name": command_name, "type": 1},
                        token="mock_token",
                        user=moderator,
                        channel_id=moderator.dm_channel.id if moderator.dm_channel else 0, # Placeholder
                        guild=guild
                    )
                    try:
                        await command.callback(command.binding, mock_interaction)
                    except Exception as e:
                        log.error(f"Error running setup command '{command_name}' from panel: {e}")

            elif action == 'send_message':
                channel = guild.get_channel(int(task.get('channel_id')))
                if channel:
                    try:
                        if task.get('is_embed'):
                            parts = task.get('content').split('|', 1)
                            title = parts[0]
                            desc = parts[1] if len(parts) > 1 else " "
                            embed = discord.Embed(title=title, description=desc, color=config.BOT_CONFIG["EMBED_COLORS"]["INFO"])
                            await channel.send(embed=embed)
                        else:
                            await channel.send(task.get('content'))
                    except discord.Forbidden:
                        log.error(f"Missing permissions to send message to channel {channel.id}.")

            self.bot.action_queue.task_done()
        except Exception as e:
            log.error(f"Error processing action queue: {e}", exc_info=True)
            # Ensure task is marked as done even on failure
            if 'task' in locals() and hasattr(self.bot, 'action_queue') and not self.bot.action_queue.empty():
                self.bot.action_queue.task_done()

    @process_action_queue.before_loop
    async def before_process_action_queue(self):
        await self.bot.wait_until_ready()

async def setup(bot: commands.Bot):
    await bot.add_cog(PanelHandlerCog(bot))