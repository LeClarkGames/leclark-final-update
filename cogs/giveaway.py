import discord
from discord import app_commands
from discord.ext import commands, tasks
import logging
import database
import config
import utils
import random
from datetime import datetime

log = logging.getLogger(__name__)

class GiveawayView(discord.ui.View):
    def __init__(self, bot: commands.Bot, giveaway_id: int, youtube_channel_url: str):
        super().__init__(timeout=None)
        self.bot = bot
        self.giveaway_id = giveaway_id

        # Add buttons if the URLs are available
        if youtube_channel_url:
            self.add_item(discord.ui.Button(label="Follow YouTube", style=discord.ButtonStyle.link, url=youtube_channel_url, emoji="▶️"))
        
        # Add the entry button with a custom ID
        entry_button = discord.ui.Button(label="Enter Giveaway", style=discord.ButtonStyle.success, custom_id=f"giveaway_enter_{giveaway_id}", emoji="🎉")
        self.add_item(entry_button)


class GiveawayCog(commands.Cog, name="Giveaway"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type == discord.InteractionType.component and interaction.data["custom_id"].startswith("giveaway_enter_"):
            giveaway_id = int(interaction.data["custom_id"].split("_")[-1])
            await self.handle_giveaway_entry(interaction, giveaway_id)

    async def handle_giveaway_entry(self, interaction: discord.Interaction, giveaway_id: int):
        """Handles a user's attempt to enter a giveaway."""
        await interaction.response.defer(ephemeral=True)
        
        giveaway = await database.get_giveaway(interaction.guild.id, giveaway_id)
        if not giveaway or not giveaway['is_active']:
            return await interaction.followup.send("This giveaway is no longer active.", ephemeral=True)
        
        has_submitted = await database.has_user_submitted_since(interaction.guild.id, interaction.user.id, giveaway['start_time'])
        if not has_submitted:
            submission_channel_id = await database.get_setting(interaction.guild.id, 'submission_channel_id')
            submission_channel = self.bot.get_channel(submission_channel_id) if submission_channel_id else None
            start_time_dt = datetime.fromisoformat(giveaway['start_time'])
            start_timestamp = int(start_time_dt.timestamp())
            
            error_message = (
                f"You must submit a track to the submissions channel "
                f"({submission_channel.mention if submission_channel else '#submissions'}) "
                f"after the giveaway started to be eligible.\n\n"
                f"**Giveaway started at:** <t:{start_timestamp}:F>"
            )
            return await interaction.followup.send(error_message, ephemeral=True)

        # Requirement 2: Check for YouTube subscription (This is a placeholder for the actual check)
        # You would need to implement the logic here using the YouTube API
        # For now, we'll assume they are subscribed if they have a verified Google account
        is_subscribed = await database.has_verified_google_account(interaction.guild.id, interaction.user.id)
        if not is_subscribed:
            return await interaction.followup.send("You must have a verified Google account to enter this giveaway. Please verify your account first.", ephemeral=True)

        # If all checks pass, add the user to the giveaway
        success = await database.add_giveaway_entrant(interaction.guild.id, giveaway_id, interaction.user.id)
        if success:
            await interaction.followup.send("🎉 You have successfully entered the giveaway! Good luck!", ephemeral=True)
        else:
            await interaction.followup.send("You have already entered this giveaway.", ephemeral=True)


    giveaway_group = app_commands.Group(name="giveaway", description="Commands for managing giveaways.")

    @giveaway_group.command(name="start", description="Starts a new giveaway.")
    @utils.has_permission("admin")
    @app_commands.describe(
        name="The name of the giveaway.",
        description="A description of the giveaway.",
        channel="The channel to post the giveaway announcement in."
    )
    async def start_giveaway(self, interaction: discord.Interaction, name: str, description: str, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True)

        giveaway_id = await database.create_giveaway(interaction.guild.id, name, description)

        youtube_channel_id = await database.get_setting(interaction.guild.id, 'giveaway_youtube_channel_id')
        youtube_url = f"https://www.youtube.com/channel/{youtube_channel_id}" if youtube_channel_id else None
        
        embed = discord.Embed(
            title=f"🎉 Giveaway: {name} 🎉",
            description=description,
            color=config.BOT_CONFIG["EMBED_COLORS"]["SUCCESS"]
        )
        embed.add_field(name="How to Enter:", value="1. **Submit a track** to the regular submissions channel.\n2. **Follow our YouTube channel.**\n3. **Click the 'Enter Giveaway' button below!**", inline=False)
        embed.set_footer(text="Good luck to everyone!")

        view = GiveawayView(self.bot, giveaway_id, youtube_url)

        try:
            message = await channel.send(embed=embed, view=view)
            await database.update_giveaway_message_id(interaction.guild.id, giveaway_id, message.id)
            await interaction.followup.send(f"✅ Giveaway has been started in {channel.mention}!", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("I don't have permission to send messages in that channel.", ephemeral=True)

    @giveaway_group.command(name="end", description="Ends the current giveaway and draws a winner.")
    @utils.has_permission("admin")
    async def end_giveaway(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        active_giveaway = await database.get_active_giveaway(interaction.guild.id)
        if not active_giveaway:
            return await interaction.followup.send("There is no active giveaway to end.", ephemeral=True)

        entrants = await database.get_giveaway_entrants(interaction.guild.id, active_giveaway['id'])
        if not entrants:
            await database.end_giveaway(interaction.guild.id, active_giveaway['id'], None)
            return await interaction.followup.send("The giveaway has ended, but there were no entrants.", ephemeral=True)

        winner_id = random.choice(entrants)
        winner = interaction.guild.get_member(winner_id)

        await database.end_giveaway(interaction.guild.id, active_giveaway['id'], winner_id)

        # Announce winner
        winner_embed = discord.Embed(
            title="🎉 Giveaway Winner! 🎉",
            description=f"Congratulations to {winner.mention if winner else f'<@{winner_id}>'} for winning the **{active_giveaway['name']}** giveaway!",
            color=discord.Color.gold()
        )
        
        # Announce in the same channel the giveaway was posted
        giveaway_message_id = active_giveaway.get('message_id')
        if giveaway_message_id:
            try:
                # Find the channel of the original message
                for channel in interaction.guild.text_channels:
                    try:
                        original_message = await channel.fetch_message(giveaway_message_id)
                        await original_message.reply(embed=winner_embed)
                        # Remove buttons from original message
                        await original_message.edit(view=None)
                        break
                    except (discord.NotFound, discord.Forbidden):
                        continue
            except Exception as e:
                log.error(f"Could not announce giveaway winner in original channel: {e}")
        else:
             # Fallback to log channel if original message not found
            log_channel_id = await database.get_setting(interaction.guild.id, 'log_channel_id')
            if log_channel_id and (log_channel := self.bot.get_channel(log_channel_id)):
                await log_channel.send(embed=winner_embed)


        await interaction.followup.send(f"✅ The giveaway has ended! The winner is {winner.mention if winner else f'<@{winner_id}>'}.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(GiveawayCog(bot))